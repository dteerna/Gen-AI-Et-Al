"""Conflict resolution and case analysis agents. Both are deterministic; the LLM never decides a clause or a number."""

from __future__ import annotations

from ..reasoning import precedence
from ..reasoning.analysis import analyze
from .base import Agent, RunContext
from .research import expand_by_supersession


class ConflictResolverAgent(Agent):
    name, reads, writes, critical = "conflict_resolver", ("case", "retrieved.clauses"), ("topic_rulings",), True

    def run(self, ctx: RunContext) -> dict:
        candidates, added = expand_by_supersession(ctx.corpus, ctx.retrieved.get("clauses", []))
        ctx.topic_rulings = precedence.resolve(candidates, ctx.case.shipment_date, ctx.case.vendor)
        outcome = {t: (r.governing.clause_id if r.governing else "UNRESOLVED") for t, r in ctx.topic_rulings.items()}
        if added:
            ctx.audit("supersession_expansion", {"added": added})
        return {"candidates": len(candidates), "added_by_supersession": added, "topics": outcome}


class AnalystAgent(Agent):
    name, reads, writes, critical = "analyst", ("case", "facts", "topic_rulings", "retrieved"), ("analysis",), True

    def run(self, ctx: RunContext) -> dict:
        case = ctx.case
        ctx.analysis = analyze(
            case, ctx.facts, ctx.topic_rulings or {}, ctx.retrieved.get("regulations", []), ctx.retrieved.get("regulations_excluded", []),
            ctx.retrieved.get("rulings", []), ctx.corpus.unreadable_for(case.vendor),
            vendor_known=ctx.corpus.vendors.canonical(case.vendor_raw) is not None, coverage_gaps=ctx.retrieved.get("gaps", []))
        d = ctx.analysis["decision"]
        return {"decision": d["decision_type"], "amount": d["amount"], "blockers": [b["kind"] for b in ctx.analysis["blockers"]],
                "flags": ctx.analysis["flags"], "required_approvals": d["required_approvals"]}
