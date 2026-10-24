"""Turn an analysis and memo into a proposal that a human can approve.

Construction is where "legally sound" is enforced mechanically. A proposal is refused if it:
  * cites a clause, regulation section, or ruling that does not exist in the corpus;
  * relies on a regulation version that was not in force on the relevant date;
  * proposes an executable decision with no governing clause or regulation behind it;
  * proposes an amount that disagrees with the calculation, or exceeds the claim;
  * proposes an unknown decision type.
The proposal id is derived from the content hash, so identical content always yields the identical id.
"""

from __future__ import annotations

import hashlib

from .corpus import Corpus
from .governance import authority

CREATED_BY = "glcc-agent"


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def build_proposal(corpus: Corpus, analysis: dict, memo: str, reg_as_of: str | None = None) -> tuple[dict | None, list[str]]:
    """Returns (proposal, []) or (None, [errors])."""
    errors: list[str] = []
    d, ev = analysis["decision"], analysis["evidence"]
    dt, amount = d["decision_type"], float(d["amount"])

    if analysis["case_id"] not in corpus.case_ids():
        errors.append(f"Unknown case {analysis['case_id']}.")
    if dt not in authority.ALLOWED_DECISIONS:
        errors.append(f"Unknown decision type {dt!r}.")
    if amount < 0:
        errors.append("Amount cannot be negative.")
    if dt in authority.SETTLEMENT_DECISIONS and amount > float(analysis["claim_amount"]) + 0.005:
        errors.append(f"Amount {amount:,.2f} exceeds the claimed amount {analysis['claim_amount']:,.2f}.")
    if analysis.get("payout") and dt in authority.SETTLEMENT_DECISIONS and abs(analysis["payout"]["total_recovery"] - amount) > 0.005:
        errors.append("The proposed amount does not match the calculated total recovery.")
    if not (memo or "").strip():
        errors.append("The memo is empty.")

    known = corpus.known_ids()
    for label, ids, universe in (("clause", ev["relied_clauses"], known["clause"]), ("regulation", ev["regulations"], known["regulation"]),
                                 ("ruling", ev["rulings"], known["ruling"])):
        unknown = [i for i in ids if i not in universe]
        if unknown:
            errors.append(f"Unknown {label} id(s): {unknown}. Only ids returned by retrieval may be cited.")
    if reg_as_of:
        for sid in ev["regulations"]:
            sec = corpus.reg_section(sid)
            if sec and not sec.in_force(reg_as_of):
                errors.append(f"Regulation {sid} was not in force on {reg_as_of}.")
    if dt in authority.EXECUTABLE_DECISIONS and not (ev["relied_clauses"] or ev["regulations"]):
        errors.append("An executable decision must cite at least one governing clause or regulation.")
    if errors:
        return None, errors

    content = {
        "case_id": analysis["case_id"], "decision_type": dt, "amount": round(amount, 2), "currency": d["currency"],
        "clause_ids": sorted(ev["relied_clauses"]), "regulation_ids": sorted(ev["regulations"]), "ruling_ids": sorted(ev["rulings"]),
        "flags": sorted(analysis["flags"]), "blockers": sorted(b["kind"] for b in analysis["blockers"]),
        "required_approvals": authority.required_approvals(dt, amount, analysis["flags"]),
        "memo_sha256": _sha(memo), "analysis_sha256": _sha(repr(sorted(analysis["decision"].items())) + repr(analysis["reasoning"])),
    }
    digest = authority.content_hash(content)
    return {"proposal_id": authority.proposal_id_for(digest), "content_hash": digest, "content": content, "created_by": CREATED_BY}, []


def escalation_analysis(case_id: str, reason: str, kind: str = "system_error", resolution: str = "escalate_to_legal") -> dict:
    """A minimal, safe analysis used when the pipeline itself fails. It routes the case to people and decides nothing."""
    decision = resolution
    return {
        "case_id": case_id, "case_type": "unknown", "vendor": "unknown", "shipment_date": "unknown", "port": "unknown",
        "claim_amount": 0.0, "currency": "USD", "containers": 1, "precedence_rules": [], "checks": {}, "payout": None, "customs": None,
        "flags": [], "discrepancies": [], "blockers": [{"kind": kind, "detail": reason, "resolution": resolution}],
        "decision": {"decision_type": decision, "amount": 0.0, "currency": "USD", "executable": False,
                     "required_approvals": authority.required_approvals(decision, 0.0, [])},
        "precedents": [], "governing": {},
        "evidence": {"clauses": [], "relied_clauses": [], "regulations": [], "rulings": [], "regulations_excluded": []},
        "reasoning": [{"step": 1, "claim": f"BLOCKER ({kind}): {reason}", "evidence": []}],
    }
