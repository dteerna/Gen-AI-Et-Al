"""Pure functions that apply governing clause terms to case facts.

Nothing here calls a model. Each check returns a dict with a `status` ("ok", "unknown", or
"not_applicable") plus the evidence behind the result, so the analysis can cite exactly which
events produced which number. "unknown" never defaults to a favorable answer: the caller turns it
into a request for information.

The settlement formula is a SAMPLE, documented in the README. Replace it with GLCC's finance rules.
"""

from __future__ import annotations

from ..models import CaseFacts, CaseRecord, Event
from .timeline import business_days_between, calendar_days_inclusive, hours_between


def _ev(e: Event | None) -> dict | None:
    return None if e is None else {"kind": e.kind, "ts": e.ts, "doc_id": e.doc_id, "quote": e.quote}


def _interval(start: Event | None, end: Event | None, required: float | None, what: str) -> dict:
    if required is None:
        return {"status": "not_applicable", "what": what}
    if start is None or end is None:
        missing = [n for n, e in (("start", start), ("end", end)) if e is None]
        return {"status": "unknown", "what": what, "required_hours": required, "missing": missing}
    elapsed = hours_between(start.ts, end.ts)
    return {"status": "ok", "what": what, "required_hours": required, "elapsed_hours": elapsed,
            "compliant": elapsed <= required, "start": _ev(start), "end": _ev(end)}


def carrier_notice_check(facts: CaseFacts, required_hours: float | None) -> dict:
    return _interval(facts.first("carrier_discovery"), facts.first("carrier_notice"), required_hours, "carrier notice to GLCC")


def insurer_notice_check(facts: CaseFacts, required_hours: float | None) -> dict:
    return _interval(facts.first("glcc_discovery"), facts.first("insurer_notice"), required_hours, "GLCC notice to insurer")


def force_majeure_check(facts: CaseFacts, written_notice_hours: float | None) -> dict:
    """A force majeure excuse needs WRITTEN notice within the window. Verbal notice never qualifies.

    The party invoking force majeure carries the burden: if no written notice is evidenced, the excuse fails.
    """
    if written_notice_hours is None:
        return {"status": "not_applicable", "what": "force majeure"}
    official = next((e for e in facts.all("closure_effective") if e.attr("source") == "port_notice"), None)
    closure = official or facts.first("closure_effective")
    if closure is None:
        return {"status": "unknown", "what": "force majeure", "missing": ["closure time"]}
    written = next((e for e in facts.all("written_notice") if e.ts >= closure.ts), None)
    verbal = facts.first("verbal_notice")
    base = {"what": "force majeure", "required_hours": written_notice_hours, "closure": _ev(closure),
            "closure_source": closure.attr("source", "unknown"), "verbal_notice": _ev(verbal)}
    if written is None:
        return {**base, "status": "ok", "written_notice": None, "elapsed_hours": None, "excused": False,
                "reason": "No written notice is evidenced; verbal notice does not satisfy the clause."}
    elapsed = hours_between(closure.ts, written.ts)
    excused = elapsed <= written_notice_hours
    return {**base, "status": "ok", "written_notice": _ev(written), "elapsed_hours": elapsed, "excused": excused,
            "reason": (f"Written notice came {elapsed:g}h after the closure, within the {written_notice_hours:g}h window."
                       if excused else f"Written notice came {elapsed:g}h after the closure, outside the {written_notice_hours:g}h window.")}


def supported_loss(case: CaseRecord, facts: CaseFacts) -> dict:
    """Settlement basis: the lower of the claim and the independent survey estimate, when a survey exists."""
    if facts.survey_amount is not None:
        amount = min(case.claim_amount, facts.survey_amount)
        basis = "lower of the claimed amount and the independent survey estimate" if facts.survey_amount < case.claim_amount \
            else "claimed amount (the survey estimate is not lower)"
        return {"amount": amount, "basis": basis, "claim": case.claim_amount, "survey": facts.survey_amount}
    return {"amount": case.claim_amount, "basis": "claimed amount (no independent estimate)", "claim": case.claim_amount, "survey": None}


def liability_cap_total(terms: dict, containers: int) -> dict:
    cap = float(terms["liability_cap"])
    per = terms.get("per", "shipment")
    total = cap * containers if per == "container" else cap
    note = f"{cap:,.0f} per container x {containers} container(s)" if per == "container" else f"{cap:,.0f} per shipment"
    return {"cap_per_unit": cap, "per": per, "containers": containers, "cap_total": total, "note": note}


def settlement(supported: float, cap_total: float, insurance: dict | None, insurer_in_time: bool | None) -> dict:
    """Sample formula:

        vendor_liability  = min(supported_loss, liability_cap_total)
        uncovered         = supported_loss - vendor_liability
        insurance_payable = max(0, uncovered - deductible) * coverage% / 100
                            (the late-notice percentage when the insurer was notified late)
        total_recovery    = vendor_liability + insurance_payable
        unrecovered       = supported_loss - total_recovery   (borne by GLCC)
    """
    vendor = min(supported, cap_total)
    uncovered = supported - vendor
    ins_pay, ded, pct, note = 0.0, 0.0, 0.0, "no insurance clause applied"
    if insurance is not None and uncovered > 0:
        ded = float(insurance["deductible"])
        pct = float(insurance["coverage_percent"] if insurer_in_time else insurance["late_notice_coverage_percent"])
        ins_pay = max(0.0, uncovered - ded) * pct / 100.0
        note = "insurer notified in time" if insurer_in_time else "insurer notified late: reduced coverage percentage applied"
    elif insurance is not None:
        note = "nothing left uncovered after the carrier's liability, so insurance is not needed"
    total = vendor + ins_pay
    return {"supported_loss": round(supported, 2), "vendor_liability": round(vendor, 2), "uncovered_after_vendor": round(uncovered, 2),
            "deductible": ded, "coverage_percent_applied": pct, "insurance_payable": round(ins_pay, 2),
            "total_recovery": round(total, 2), "unrecovered": round(supported - total, 2), "insurance_note": note}


def customs_check(case: CaseRecord, facts: CaseFacts, submission_business_days: int | None, required_docs: list[str],
                  allocation_terms: dict) -> dict:
    hold = facts.first("hold_notice")
    if hold is None:
        return {"status": "unknown", "what": "customs hold", "missing": ["hold notice"]}
    complete = facts.first("submission_complete")
    missing_filings = list(facts.missing_required_filings)
    base = {"what": "customs hold", "status": "ok", "hold_notice": _ev(hold), "missing_filings": missing_filings,
            "required_documents": required_docs, "allowed_business_days": submission_business_days}

    # Who bears the detention charges?
    if facts.fault == "glcc" and "documentation_error" in allocation_terms:
        base.update(responsible_party=allocation_terms["documentation_error"], cause="documentation error by GLCC or its shipper")
    elif facts.fault == "carrier" and "carrier_caused" in allocation_terms:
        base.update(responsible_party=allocation_terms["carrier_caused"], cause="carrier-caused hold")
    else:
        base.update(responsible_party=None, cause="cause of the hold not established")

    if complete is None:
        return {**base, "complete": False, "submission_complete": None, "compliant": None, "business_days_used": None,
                "days_charged": None, "detention_charges": None}

    used = business_days_between(hold.ts, complete.ts)
    days = calendar_days_inclusive(hold.ts, complete.ts)
    compliant = None if submission_business_days is None else used <= submission_business_days
    return {**base, "complete": True, "submission_complete": _ev(complete), "business_days_used": used, "compliant": compliant,
            "days_charged": days, "detention_charges": round(days * case.detention_charge_per_day, 2)}
