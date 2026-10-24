"""Draft-and-review loop.

DrafterAgent renders the memo deterministically from the analysis. With an LLM it may add a short narrative,
which is accepted only if the critic's text checks pass (real ids, no invented figures, no claim that anything
was executed). CriticAgent then reviews the whole memo; if an LLM narrative caused a failure it is dropped and
the deterministic memo is reviewed again. A memo that still fails review does not become a proposal.
"""

from __future__ import annotations

import json

from ..llm.base import LLMError
from . import critic
from .base import Agent, RunContext
from .memo import render_memo

SCHEMA = {"type": "object", "required": ["summary", "explanation"],
          "properties": {"summary": {"type": "string", "maxLength": 600}, "explanation": {"type": "string", "maxLength": 1200}}}
SYSTEM = (
    "You write a short narrative for human approvers of a logistics exception decision. Use ONLY the facts, ids, and numbers "
    "in the JSON provided. Never introduce a new figure or a clause id. Never say that anything has been paid, executed, "
    "released, or submitted: this is a proposal that still needs human approval. Output JSON only: "
    '{"summary": "...", "explanation": "..."} (summary: 1-2 sentences; explanation: a short paragraph).'
)


def _brief(a: dict) -> dict:
    return {"case_id": a["case_id"], "case_type": a["case_type"], "vendor": a["vendor"], "decision": a["decision"],
            "blockers": a["blockers"], "reasoning": [r["claim"] for r in a["reasoning"]][:20], "payout": a["payout"],
            "evidence_ids": a["evidence"]["clauses"] + a["evidence"]["regulations"] + a["evidence"]["rulings"]}


class DrafterAgent(Agent):
    name, reads, writes, critical = "drafter", ("analysis",), ("memo",), True

    def run(self, ctx: RunContext) -> dict:
        a, narrative, meta = ctx.analysis, None, {"narrative": "none"}
        if ctx.llm is not None:
            try:
                out = ctx.llm.complete_json(system=SYSTEM, user=json.dumps(_brief(a), default=str), schema=SCHEMA)
                issues = critic.check_text(out["summary"] + " " + out["explanation"], a, ctx.corpus.known_ids())
                if issues:
                    meta = {"narrative": "rejected", "reasons": issues}
                    ctx.audit("llm_output_rejected", {"agent": self.name, "reasons": issues})
                else:
                    narrative, meta = out, {"narrative": "llm"}
            except LLMError as exc:
                meta = {"narrative": "unavailable", "reasons": [str(exc)]}
                ctx.audit("llm_fallback", {"agent": self.name, "reason": str(exc)})
        ctx.memo, ctx.memo_meta = render_memo(a, narrative), meta
        return {"memo_chars": len(ctx.memo), **{k: v for k, v in meta.items() if k == "narrative"}}


class CriticAgent(Agent):
    name, reads, writes, critical = "critic", ("analysis", "memo"), ("memo_meta",), True

    def run(self, ctx: RunContext) -> dict:
        known = ctx.corpus.known_ids()
        issues = critic.review_memo(ctx.memo, ctx.analysis, known)
        redrafted = False
        if issues and ctx.memo_meta.get("narrative") == "llm":
            ctx.audit("llm_output_rejected", {"agent": self.name, "reasons": issues})
            ctx.memo_meta = {"narrative": "dropped_by_critic", "reasons": issues}
            ctx.memo, redrafted = render_memo(ctx.analysis), True
            issues = critic.review_memo(ctx.memo, ctx.analysis, known)
        ctx.memo_meta.update(critic_passed=not issues, critic_issues=issues)
        return {"passed": not issues, "issues": issues, "redrafted_without_narrative": redrafted}
