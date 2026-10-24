"""The supervisor. It owns ordering, parallelism, timeouts, failure policy, tracing, and persistence.

Pipeline (blackboard pattern, see agents/base.py):

    intake -> planner -> [clause | regulation | precedent researchers in parallel]
           -> conflict_resolver -> analyst -> drafter -> critic -> proposal

Failure policy (the point of having an orchestrator):
  * unknown case / unreadable case file ......... the run FAILS; nothing is proposed; the failure is audited
  * a researcher fails or times out ............. the pipeline continues, the gap is recorded, and the analyst turns
                                                  it into an escalation (a precedent failure only loses precedents)
  * any later agent raises, or the critic rejects
    the memo, or the proposal builder refuses ... the run ends in a SAFE ESCALATION: a non-executable proposal that routes
                                                  the case to legal counsel, with the reason
In every branch the outcome is either a correct proposal or a handoff to people. It is never a silent guess.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from time import perf_counter

from ..corpus import Corpus
from ..governance.store import Store
from ..proposals import build_proposal, escalation_analysis
from ..retrieval.retrievers import Retrievers
from .analysis_agents import AnalystAgent, ConflictResolverAgent
from .base import Agent, RunContext
from .drafting import CriticAgent, DrafterAgent
from .intake import IntakeAgent
from .memo import render_memo
from .research import ClauseResearcher, PlannerAgent, PrecedentResearcher, RegulationResearcher

PIPELINE_SERIAL_1 = [IntakeAgent(), PlannerAgent()]
RESEARCHERS = [ClauseResearcher(), RegulationResearcher(), PrecedentResearcher()]
PIPELINE_SERIAL_2 = [ConflictResolverAgent(), AnalystAgent(), DrafterAgent(), CriticAgent()]


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Orchestrator:
    def __init__(self, corpus: Corpus, store: Store | None = None, llm=None, research_timeout_s: float = 60.0):
        self.corpus, self.store, self.llm = corpus, store, llm
        self.research_timeout_s = research_timeout_s
        self.retrievers = Retrievers.build(corpus)

    # ---- one agent -------------------------------------------------------------------------------------------------
    def _exec(self, ctx: RunContext, agent: Agent) -> dict:
        t0 = perf_counter()
        entry = {"agent": agent.name, "reads": list(agent.reads), "writes": list(agent.writes), "status": "ok"}
        try:
            entry["summary"] = agent.run(ctx)
        except Exception as exc:                       # an agent may fail; the orchestrator decides what that means
            entry.update(status="failed", error=f"{type(exc).__name__}: {exc}", critical=agent.critical)
        entry["ms"] = round((perf_counter() - t0) * 1000, 1)
        ctx.audit("agent_completed", {"agent": agent.name, "status": entry["status"], "ms": entry["ms"],
                                      "summary_digest": _digest(entry.get("summary") or entry.get("error"))})
        return entry

    def _research(self, ctx: RunContext) -> list[dict]:
        """Run the researchers concurrently. Results are appended in a fixed order so traces are reproducible."""
        entries: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=len(RESEARCHERS), thread_name_prefix="research") as pool:
            futures = {a.name: pool.submit(self._exec, ctx, a) for a in RESEARCHERS}
            for name, fut in futures.items():
                try:
                    entries[name] = fut.result(timeout=self.research_timeout_s)
                except FutureTimeout:
                    entries[name] = {"agent": name, "status": "failed", "error": f"timed out after {self.research_timeout_s:g}s", "critical": False}
        for a in RESEARCHERS:                           # translate research failures into explicit, auditable gaps
            e = entries[a.name]
            if e["status"] != "ok":
                if a.name == "clause_researcher":
                    ctx.retrieved.setdefault("gaps", []).append(f"Clause retrieval failed ({e['error']}); contract terms could not be established.")
                elif a.name == "regulation_researcher" and ctx.case.case_type == "customs_hold":
                    ctx.retrieved.setdefault("gaps", []).append(f"Regulation retrieval failed ({e['error']}); the governing regulation could not be established.")
                else:
                    ctx.notes.append(f"{a.name} failed ({e['error']}); continuing without it.")
        return [entries[a.name] for a in RESEARCHERS]

    # ---- the run ----------------------------------------------------------------------------------------------------
    def run(self, case_id: str, actor: str = "agent:glcc") -> dict:
        ctx = RunContext(corpus=self.corpus, case_id=case_id, llm=self.llm, retrievers=self.retrievers)
        started = _now()
        audit_log: list[dict] = []

        def audit(event: str, payload: dict):
            rec = (self.store.append_audit(actor, event, payload, run_id=ctx.run_id, case_id=case_id) if self.store
                   else {"event": event, "payload": payload})
            audit_log.append(rec)
            return rec

        ctx.audit = audit
        audit("run_started", {"mode": ctx.mode})

        def finish(status: str, error: str | None = None) -> dict:
            decision = ctx.analysis["decision"] if ctx.analysis else None
            result = {
                "run_id": ctx.run_id, "case_id": case_id, "mode": ctx.mode, "status": status, "error": error,
                "started_at": started, "finished_at": _now(),
                "decision": decision["decision_type"] if decision else None, "amount": decision["amount"] if decision else None,
                "proposal": ctx.proposal, "memo": ctx.memo, "memo_meta": ctx.memo_meta, "analysis": ctx.analysis,
                "trace": ctx.trace, "notes": ctx.notes, "retrieval": {k: v for k, v in ctx.retrieved.items() if k.endswith("provenance")},
                "llm_stats": getattr(self.llm, "stats", None), "audit_events": [r["event"] for r in audit_log],
            }
            if self.store:
                self.store.save_run({**result, "proposal_id": ctx.proposal["proposal_id"] if ctx.proposal else None})
            return result

        # 1. intake and planning: if these fail there is nothing safe to propose
        for agent in PIPELINE_SERIAL_1:
            ctx.trace.append(e := self._exec(ctx, agent))
            if e["status"] != "ok":
                audit("run_failed", {"agent": agent.name, "error": e["error"]})
                return finish("failed", f"{agent.name}: {e['error']}")

        # 2. parallel retrieval
        ctx.trace.extend(self._research(ctx))

        # 3. reasoning, drafting, review
        for agent in PIPELINE_SERIAL_2:
            ctx.trace.append(e := self._exec(ctx, agent))
            if e["status"] != "ok":
                return self._escalate(ctx, audit, finish, actor, f"{agent.name} failed: {e['error']}", "system_error")

        # 4. a memo the critic rejected must not become a proposal
        if not ctx.memo_meta.get("critic_passed"):
            return self._escalate(ctx, audit, finish, actor, "The memo failed independent review: " + "; ".join(ctx.memo_meta.get("critic_issues", [])), "memo_failed_review")

        # 5. proposal
        proposal, errors = build_proposal(self.corpus, ctx.analysis, ctx.memo, ctx.retrieved.get("reg_as_of"))
        ctx.trace.append({"agent": "proposal_builder", "status": "ok" if proposal else "rejected", "reads": ["analysis", "memo"],
                          "writes": ["proposal"], "summary": {"errors": errors}})
        if not proposal:
            return self._escalate(ctx, audit, finish, actor, "The proposal builder refused: " + "; ".join(errors), "proposal_rejected")
        ctx.proposal = self._persist(proposal, ctx, actor)
        audit("run_completed", {"decision": ctx.analysis["decision"]["decision_type"], "proposal_id": proposal["proposal_id"]})
        return finish("completed")

    def _persist(self, proposal: dict, ctx: RunContext, actor: str) -> dict:
        if self.store:
            saved, _created = self.store.save_proposal(proposal, ctx.run_id, actor)
            return saved
        return proposal

    def _escalate(self, ctx: RunContext, audit, finish, actor: str, reason: str, kind: str) -> dict:
        """Safe failure: route the case to legal counsel with the reason, and propose nothing executable."""
        audit("run_escalated", {"kind": kind, "reason": reason[:300]})
        ctx.memo_meta["original_analysis_decision"] = ctx.analysis["decision"] if ctx.analysis else None
        ctx.analysis = escalation_analysis(ctx.case_id, reason, kind)
        ctx.memo = render_memo(ctx.analysis)
        ctx.memo_meta.update(critic_passed=True, escalated=True)
        proposal, errors = build_proposal(self.corpus, ctx.analysis, ctx.memo)
        if not proposal:                                # cannot happen for a non-executable decision; fail loudly if it does
            audit("run_failed", {"error": "; ".join(errors)})
            return finish("failed", "; ".join(errors))
        ctx.proposal = self._persist(proposal, ctx, actor)
        ctx.trace.append({"agent": "safe_escalation", "status": "ok", "summary": {"kind": kind, "reason": reason[:300]}})
        return finish("escalated", reason)
