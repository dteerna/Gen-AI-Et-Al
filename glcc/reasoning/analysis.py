"""Assemble the full analysis of one case: governing terms + facts -> checks -> decision.

Design rules:
  * Anything that moves money or releases cargo is computed here, in code.
  * "I cannot tell" is a first-class outcome. A missing document, an unreadable vendor agreement, an
    unresolved clause conflict, or a missing required clause topic becomes a BLOCKER, and a blocker
    produces `escalate_to_legal` or `request_information`, never a guess.
  * Every conclusion is appended to `reasoning` with the ids and quotes that support it.
"""

from __future__ import annotations

from ..governance.authority import SETTLEMENT_DECISIONS, required_approvals
from ..models import CaseFacts, CaseRecord, RegSection, Ruling
from .liability import (carrier_notice_check, customs_check, force_majeure_check, insurer_notice_check,
                        liability_cap_total, settlement, supported_loss)
from .precedence import RULES, TopicRuling

NEEDED_TOPICS = {
    "freight_damage": ["liability_cap", "insurance_coverage"],
    "vessel_delay": ["force_majeure"],
    "customs_hold": ["detention_demurrage"],
}
REG_TOPIC = {"customs_hold": "customs_compliance_hold"}

_CONSISTENT = {
    "escalate_to_legal": ("escalated",),
    "clear_customs_hold": ("hold_cleared",),
    "deny_claim": ("denied",),
    "approve_settlement": ("paid", "allowed", "applied", "reduced"),
    "partial_settlement": ("paid", "allowed", "applied", "reduced"),
}


class _Builder:
    def __init__(self) -> None:
        self.reasoning: list[dict] = []
        self.blockers: list[dict] = []

    def step(self, claim: str, evidence: list[str] | None = None) -> None:
        self.reasoning.append({"step": len(self.reasoning) + 1, "claim": claim, "evidence": evidence or []})

    def block(self, kind: str, detail: str, resolution: str) -> None:
        """resolution: 'escalate_to_legal' or 'request_information'."""
        self.blockers.append({"kind": kind, "detail": detail, "resolution": resolution})
        self.step(f"BLOCKER ({kind}): {detail}")


def _gov(rulings: dict[str, TopicRuling], topic: str) -> TopicRuling | None:
    return rulings.get(topic)


def _require_topic(b: _Builder, rulings: dict[str, TopicRuling], topic: str) -> TopicRuling | None:
    r = _gov(rulings, topic)
    if r is None:
        b.block("missing_clause_topic", f"No clause on {topic.replace('_', ' ')} was found for this vendor.", "escalate_to_legal")
        return None
    if r.needs_human:
        b.block("unresolved_clause_conflict", f"{topic.replace('_', ' ')}: {', '.join(r.unresolved) or 'no clause'} conflict and the "
                "precedence rules cannot decide between them.", "escalate_to_legal")
        return None
    return r


def _consistency(decision: str, precedent: Ruling) -> str:
    return "consistent outcome" if any(k in precedent.outcome for k in _CONSISTENT.get(decision, ())) else "different outcome"


def analyze(case: CaseRecord, facts: CaseFacts, rulings: dict[str, TopicRuling], regulation: list[RegSection], reg_excluded: list[dict],
            precedents: list[Ruling], unreadable_vendor_docs: list[dict], vendor_known: bool, coverage_gaps: list[str] | None = None) -> dict:
    b = _Builder()
    out: dict = {"case_id": case.case_id, "case_type": case.case_type, "vendor": case.vendor, "shipment_date": case.shipment_date,
                 "port": case.port, "claim_amount": case.claim_amount, "currency": case.currency, "containers": case.containers,
                 "precedence_rules": RULES, "checks": {}, "payout": None, "customs": None}

    # ---- preconditions -------------------------------------------------------------------------------------
    if not vendor_known:
        b.block("unknown_vendor", f"Vendor {case.vendor_raw!r} is not in the vendor registry, so vendor-specific terms cannot be ruled out.",
                "request_information")
    for u in unreadable_vendor_docs:
        b.block("unreadable_vendor_document", f"{u['doc_id']} exists for {case.vendor} but could not be read ({u['reason']}); "
                "its terms may override the master agreement, so the master agreement is not assumed to apply.", "request_information")
    for gap in coverage_gaps or []:
        b.block("retrieval_gap", gap, "escalate_to_legal")
    for doc in facts.missing_documents:
        b.block("missing_required_document", f"Required evidence is missing from the file: {doc}.", "request_information")
    if facts.unreadable_documents:
        b.step("Unreadable files were not analyzed and were not relied on: " + ", ".join(facts.unreadable_documents) + ".")
    for d in facts.discrepancies:
        b.step(f"DISCREPANCY ({d.kind}): {d.description} Resolution: {d.resolution}", d.doc_ids)

    # ---- clause precedence -------------------------------------------------------------------------------------
    for topic, r in rulings.items():
        for s in r.steps:
            b.step(s, ([r.governing.clause_id] if r.governing else r.unresolved) + [o["clause_id"] for o in r.overridden])
        for o in r.overridden:
            b.step(f"{o['clause_id']} does not govern: {o['reason']}.", [o["clause_id"]])
    for topic in NEEDED_TOPICS.get(case.case_type, []):
        _require_topic(b, rulings, topic)

    # ---- case-type specific application ---------------------------------------------------------------------------
    amount, decision, relied_regs = 0.0, None, []
    clean = not b.blockers

    if case.case_type == "freight_damage" and clean:
        cap, ins = rulings["liability_cap"].governing, rulings["insurance_coverage"].governing
        notif = rulings.get("notification_deadline")
        nreq = notif.governing.terms.get("notification_hours") if notif and notif.governing else None
        out["checks"]["carrier_notice"] = carrier_notice_check(facts, nreq)
        out["checks"]["insurer_notice"] = insurer_notice_check(facts, ins.terms.get("insurer_notice_hours"))
        cn, ic = out["checks"]["carrier_notice"], out["checks"]["insurer_notice"]
        if cn["status"] == "ok":
            b.step(f"Carrier notified GLCC {cn['elapsed_hours']:g}h after discovery against a {cn['required_hours']:g}h requirement "
                   f"({'compliant' if cn['compliant'] else 'LATE'}). The clauses state no consequence for late notice on direct loss.",
                   [notif.governing.clause_id, cn["start"]["doc_id"], cn["end"]["doc_id"]])
        if ic["status"] != "ok":
            b.block("missing_event", "The date GLCC notified the insurer cannot be established from the file.", "request_information")
        else:
            b.step(f"GLCC notified the insurer {ic['elapsed_hours']:g}h after discovery against a {ic['required_hours']:g}h window "
                   f"({'in time' if ic['compliant'] else 'LATE: reduced coverage applies'}).", [ins.clause_id, ic["end"]["doc_id"]])
        if not b.blockers:
            sl = supported_loss(case, facts)
            capinfo = liability_cap_total(cap.terms, case.containers)
            pay = settlement(sl["amount"], capinfo["cap_total"], ins.terms, ic["compliant"])
            out["payout"] = {**pay, "supported_loss_basis": sl["basis"], "cap": capinfo, "liability_clause_id": cap.clause_id,
                             "insurance_clause_id": ins.clause_id}
            b.step(f"Settlement basis is {sl['amount']:,.2f} {case.currency} ({sl['basis']}).", [cap.clause_id])
            b.step(f"Carrier liability = min({sl['amount']:,.2f}, cap {capinfo['cap_total']:,.0f} [{capinfo['note']}]) = {pay['vendor_liability']:,.2f}.", [cap.clause_id])
            b.step(f"Insurance pays {pay['insurance_payable']:,.2f} ({pay['insurance_note']}); total recovery {pay['total_recovery']:,.2f}; "
                   f"unrecovered {pay['unrecovered']:,.2f} borne by GLCC.", [ins.clause_id])
            amount = pay["total_recovery"]
            decision = "approve_settlement" if pay["unrecovered"] <= 0.005 else "partial_settlement"

    elif case.case_type == "vessel_delay" and clean:
        fm = rulings["force_majeure"].governing
        chk = force_majeure_check(facts, fm.terms.get("written_notice_hours"))
        out["checks"]["force_majeure"] = chk
        if chk["status"] != "ok":
            b.block("missing_event", f"Force majeure cannot be assessed: missing {', '.join(chk.get('missing', []))}.", "request_information")
        else:
            ev = list(dict.fromkeys([fm.clause_id] + [x["doc_id"] for x in (chk.get("closure"), chk.get("written_notice"), chk.get("verbal_notice")) if x]))
            b.step(chk["reason"] + f" Closure time taken from the {chk['closure_source']}.", ev)
            if facts.notes:
                for n in facts.notes:
                    b.step(n)
            if chk["excused"]:
                decision, amount = "deny_claim", 0.0
                b.step("Force majeure excuses the delay, so the delay claim is denied.", [fm.clause_id])
            else:
                decision, amount = "approve_settlement", float(case.claim_amount)
                b.step(f"Force majeure does not excuse the delay, so the carrier is liable for the delay cost of {amount:,.2f} {case.currency}. "
                       "No delay-specific cap clause exists in the corpus; confirm none applies.", [fm.clause_id])

    elif case.case_type == "customs_hold" and clean:
        det = rulings["detention_demurrage"].governing
        if not regulation:
            b.block("no_regulation_in_force", f"No regulation on customs holds was in force at {case.port} on the hold date.", "escalate_to_legal")
        else:
            sub = next((s.terms["submission_business_days"] for s in regulation if "submission_business_days" in s.terms), None)
            rel = next((s.terms["release_business_days"] for s in regulation if "release_business_days" in s.terms), None)
            docs = next((s.terms["required_documents"] for s in regulation if "required_documents" in s.terms), [])
            relied_regs = [s.section_id for s in regulation]
            cu = customs_check(case, facts, sub, docs, det.terms)
            out["customs"] = {**cu, "release_business_days": rel}
            if cu["status"] != "ok":
                b.block("missing_event", "No hold notice was found, so the hold timeline cannot be assessed.", "request_information")
            else:
                version = regulation[0].version
                b.step(f"{regulation[0].reg_id} version {version} was in force on the hold date; its deadline is {sub} business days "
                       f"(excluded: {', '.join(e['id'] for e in reg_excluded) or 'none'}).", relied_regs)
                if not cu["complete"]:
                    b.block("incomplete_submission", f"The port has not confirmed a complete submission; missing filing(s): "
                            f"{', '.join(cu['missing_filings']) or 'unknown'}.", "request_information")
                elif cu["compliant"] is False:
                    b.block("late_customs_submission", f"The submission took {cu['business_days_used']} business days against a limit of {sub}.",
                            "escalate_to_legal")
                elif cu["responsible_party"] is None:
                    b.block("unknown_fault", "The cause of the hold is not established, so detention charges cannot be allocated.", "request_information")
                else:
                    b.step(f"Submission completed in {cu['business_days_used']} business day(s) against a limit of {sub}: compliant.", relied_regs)
                    b.step(f"Cause: {cu['cause']}. Under {det.clause_id} the detention charges ({cu['days_charged']} day(s) x "
                           f"{case.detention_charge_per_day:,.0f} = {cu['detention_charges']:,.2f} {case.currency}) are borne by {cu['responsible_party'].upper()}.",
                           [det.clause_id])
                    decision, amount = "clear_customs_hold", 0.0

    # ---- decision ------------------------------------------------------------------------------------------------------
    if b.blockers:
        decision = "escalate_to_legal" if any(x["resolution"] == "escalate_to_legal" for x in b.blockers) else "request_information"
        amount = 0.0
        out["payout"] = None                      # never leave a computed amount next to a blocked decision
    flags = []
    if facts.security_flags:
        flags.append("injection_suspected")
        b.step(f"SECURITY: instruction-like text was found in {len({f['doc_id'] for f in facts.security_flags})} document(s) "
               "and treated strictly as untrusted data. Legal review has been added.", sorted({f["doc_id"] for f in facts.security_flags}))
    if any(d.material for d in facts.discrepancies):
        flags.append("material_discrepancy")

    out["flags"] = flags
    out["discrepancies"] = [d.to_dict() for d in facts.discrepancies]
    out["blockers"] = b.blockers
    out["decision"] = {"decision_type": decision, "amount": round(amount, 2), "currency": case.currency,
                       "required_approvals": required_approvals(decision, amount, flags),
                       "executable": decision in {"approve_settlement", "partial_settlement", "deny_claim", "clear_customs_hold"}}

    # ---- precedents, evidence ids ------------------------------------------------------------------------------------------
    applied = {r.governing.clause_id for r in rulings.values() if r.governing} | {o["clause_id"] for r in rulings.values() for o in r.overridden}
    out["precedents"] = []
    for p in precedents:
        overlap = sorted(set(p.clauses_applied) & applied)
        out["precedents"].append({"ruling_id": p.ruling_id, "outcome": p.outcome, "summary": p.summary, "rationale": p.rationale,
                                  "shared_clauses": overlap, "relationship": _consistency(decision, p)})
        b.step(f"Precedent {p.ruling_id} ({p.outcome}): {_consistency(decision, p)}"
               + (f"; shares {', '.join(overlap)}." if overlap else "."), [p.ruling_id])

    out["governing"] = {t: r.to_dict() for t, r in rulings.items()}
    governing_ids = sorted(r.governing.clause_id for r in rulings.values() if r.governing and r.topic in
                           NEEDED_TOPICS.get(case.case_type, []) + ["notification_deadline"])
    out["evidence"] = {
        "clauses": sorted(applied | {u for r in rulings.values() for u in r.unresolved}),
        "relied_clauses": governing_ids,
        "regulations": sorted(relied_regs),
        "rulings": [p.ruling_id for p in precedents],
        "regulations_excluded": reg_excluded,
    }
    out["reasoning"] = b.reasoning
    return out
