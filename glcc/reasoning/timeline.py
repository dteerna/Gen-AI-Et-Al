"""Turn messy case documents into structured, source-referenced facts.

This is the deterministic baseline extractor: regular expressions over the document formats in the
sample data (emails with RFC 2822 headers, timestamped chat lines, port notices, survey reports,
claim-form PDFs). Every extracted fact carries the document id and a verbatim quote, so it can be
checked against its source. In LLM mode the Intake agent adds events this extractor missed, but only
those whose quote is found verbatim in the cited document (see agents/intake.py).

Assumptions (stated so they can be challenged):
  * All timestamps are UTC. Email Date headers are converted to UTC; chat logs are assumed UTC.
  * GLCC "discovers" a loss when the carrier first notifies it.
  * Business days are Monday to Friday; public holidays are not modeled.
  * Evidence hierarchy for conflicts: official port notice > system timestamp (email header, chat log)
    > narrative assertion in the body of a message.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from ..governance.security import injection_matches
from ..models import Assertion, CaseFacts, CaseRecord, Discrepancy, Document, Event
from ..ingest.common import normalize_ws

EVENT_KINDS = {
    "carrier_discovery", "carrier_notice", "glcc_discovery", "insurer_notice", "closure_effective",
    "written_notice", "verbal_notice", "hold_notice", "submission_sent", "submission_complete",
    "admission_glcc_error", "missing_document", "stated_amount",
}
KNOWN_FILINGS = ("commercial invoice", "certificate of origin", "packing list", "bill of lading")
REQUIRED_KINDS = {"freight_damage": [("survey", "survey_report")], "vessel_delay": [("notice", "port_notice")], "customs_hold": []}

_CHAT = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*([^:]{1,60}):\s*(.*)$")
_WHEN = r"(\d{4}-\d{2}-\d{2})\s*(?:at\s*)?(\d{2}:\d{2})"
_WORD_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}


# ---- time helpers ------------------------------------------------------------------------------------

def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def hours_between(start: str, end: str) -> float:
    return round((parse_ts(end) - parse_ts(start)).total_seconds() / 3600.0, 2)


def _as_date(s: str) -> date:
    return parse_ts(s).date() if " " in s else date.fromisoformat(s)


def business_days_between(start: str, end: str) -> int:
    """Weekdays in the half-open interval (start date, end date]. Same-day returns 0."""
    d, d1, n = _as_date(start), _as_date(end), 0
    while d < d1:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def calendar_days_inclusive(start: str, end: str) -> int:
    return (parse_ts(end).date() - parse_ts(start).date()).days + 1


# ---- helpers -------------------------------------------------------------------------------------------

def document_haystack(doc: Document) -> str:
    """Everything a quote may legitimately be drawn from: the body plus email header fields."""
    header = " ".join(str(v) for v in doc.meta.values())
    return normalize_ws(f"{doc.text} {header}")


def quote_is_grounded(doc: Document | None, quote: str) -> bool:
    """True if `quote` appears verbatim (whitespace-normalized, case-insensitive) in the document."""
    return bool(doc and quote and normalize_ws(quote).lower() in document_haystack(doc).lower())


def role_of(who: str, vendor: str) -> str:
    n = (who or "").lower()
    vtoken = vendor.split()[0].lower() if vendor else "~"
    if n.startswith("glcc") or "@glcc" in n:
        return "glcc"
    if vtoken in n:
        return "carrier"
    if "port" in n:
        return "port"
    return "other"


def _quote(text: str, pos: int, limit: int = 220) -> str:
    """The sentence of `text` containing position `pos` (a verbatim substring, so it can be re-verified)."""
    start = text.rfind(". ", 0, pos)
    start = 0 if start < 0 else start + 2
    end = text.find(". ", pos)
    end = len(text) if end < 0 else end + 1
    return text[start:end].strip()[:limit]


def _hours_from_claim(num: str, unit: str) -> float:
    n = _WORD_NUM.get(num.lower()) or float(num)
    return n * (24 if unit.lower() == "day" else 1)


# ---- per-document extraction ----------------------------------------------------------------------------

def _email_events(doc: Document, vendor: str) -> tuple[list[Event], list[Assertion]]:
    events: list[Event] = []
    assertions: list[Assertion] = []
    body = normalize_ws(doc.text)
    sent = doc.meta.get("date", "")
    role = role_of(doc.meta.get("from", ""), vendor)

    if role == "carrier":
        m = re.search(r"discovered[^.]*?" + _WHEN, body, re.I)
        if m:
            events.append(Event(f"{m.group(1)} {m.group(2)}", "carrier_discovery", "carrier", doc.doc_id, _quote(body, m.start())))
        m = re.search(r"formally\s+notif|hereby\s+notif", body, re.I)
        if m and sent:
            events.append(Event(sent, "carrier_notice", "carrier", doc.doc_id, _quote(body, m.start())))
        m = re.search(r"clos(?:ed|ure)[^.]*?" + _WHEN, body, re.I)
        if m:
            events.append(Event(f"{m.group(1)} {m.group(2)}", "closure_effective", "carrier", doc.doc_id, _quote(body, m.start()),
                                (("source", "carrier_report"),)))
        for m in re.finditer(r"within\s+(a|an|one|two|three|\d+)\s+(day|hour)s?", body, re.I):
            assertions.append(Assertion(doc.doc_id, doc.meta.get("from", ""), "timeliness_claim",
                                        _hours_from_claim(m.group(1), m.group(2)), _quote(body, m.start())))
    elif role == "port":
        if re.search(r"\bhold\b", doc.meta.get("subject", ""), re.I) and sent:
            pos = body.lower().find("hold")
            events.append(Event(sent, "hold_notice", "port", doc.doc_id, _quote(body, pos) if pos >= 0 else doc.meta.get("subject", "")))
        for filing in KNOWN_FILINGS:
            m = re.search(rf"(?:\bno\b|missing|without|lacks?)\s+(?:an?\s+|the\s+)?{filing}", body, re.I)
            if m and sent:
                events.append(Event(sent, "missing_document", "port", doc.doc_id, _quote(body, m.start()), (("filing", filing),)))
    return events, assertions


def _chat_events(doc: Document, vendor: str) -> tuple[list[Event], list[Assertion]]:
    events: list[Event] = []
    assertions: list[Assertion] = []
    for raw in doc.text.splitlines():
        m = _CHAT.match(raw.strip())
        if not m:
            continue
        ts, speaker, msg = m.group(1), m.group(2).strip(), m.group(3)
        role, low = role_of(speaker, vendor), msg.lower()
        q = f"[{ts}] {speaker}: {msg}"
        if role == "glcc" and "notified the cargo insurer" in low:
            events.append(Event(ts, "insurer_notice", "glcc", doc.doc_id, q))
        if role == "carrier" and "written notice" in low and re.search(r"sending|sent|issued|submitting", low):
            events.append(Event(ts, "written_notice", "carrier", doc.doc_id, q))
        if role == "carrier" and re.search(r"verbal|voice call", low):
            events.append(Event(ts, "verbal_notice", "carrier", doc.doc_id, q))
        if role == "glcc" and "submitted to the port" in low:
            events.append(Event(ts, "submission_sent", "glcc", doc.doc_id, q))
        if role == "port" and ("file now complete" in low or ("submission received" in low and "complete" in low)):
            events.append(Event(ts, "submission_complete", "port", doc.doc_id, q))
        if role == "glcc" and re.search(r"our error|missed by our|our export team", low):
            events.append(Event(ts, "admission_glcc_error", "glcc", doc.doc_id, q, (("cause", "documentation"),)))
        sa = re.search(r"delay cost[^.]*?USD\s*([\d,]+)", msg, re.I)
        if sa:
            events.append(Event(ts, "stated_amount", role, doc.doc_id, q, (("amount", sa.group(1).replace(",", "")),)))
        for am in re.finditer(r"within\s+(a|an|one|two|three|\d+)\s+(day|hour)s?", msg, re.I):
            if role == "carrier":
                assertions.append(Assertion(doc.doc_id, speaker, "timeliness_claim", _hours_from_claim(am.group(1), am.group(2)), q))
    return events, assertions


def _notice_events(doc: Document) -> list[Event]:
    body = normalize_ws(doc.text)
    m = re.search(r"effective\s+" + _WHEN, body, re.I)
    if m and re.search(r"clos", body, re.I):
        return [Event(f"{m.group(1)} {m.group(2)}", "closure_effective", "port", doc.doc_id, _quote(body, m.start()),
                      (("source", "port_notice"),))]
    return []


def extract_events(case: CaseRecord) -> tuple[list[Event], list[Assertion]]:
    events: list[Event] = []
    assertions: list[Assertion] = []
    for doc in case.documents:
        if not doc.readable:
            continue
        if doc.kind == "email":
            e, a = _email_events(doc, case.vendor)
        elif doc.kind == "chat":
            e, a = _chat_events(doc, case.vendor)
        elif doc.kind == "notice":
            e, a = _notice_events(doc), []
        else:
            continue
        events.extend(e)
        assertions.extend(a)
    return events, assertions


# ---- discrepancies ----------------------------------------------------------------------------------------

def find_discrepancies(case: CaseRecord, facts: CaseFacts) -> list[Discrepancy]:
    out: list[Discrepancy] = []

    # 1. A narrative claim of timeliness versus the timestamps
    closure = next((e for e in facts.all("closure_effective") if e.attr("source") == "port_notice"), None) or facts.first("closure_effective")
    for a in facts.assertions:
        measured = start = end = None
        if case.case_type == "vessel_delay" and closure:
            wn = next((e for e in facts.all("written_notice") if e.ts >= closure.ts), None)
            if wn:
                start, end, measured = closure, wn, hours_between(closure.ts, wn.ts)
        elif case.case_type in ("freight_damage", "customs_hold"):
            d, n = facts.first("carrier_discovery"), facts.first("carrier_notice")
            if d and n:
                start, end, measured = d, n, hours_between(d.ts, n.ts)
        if measured is not None and measured > a.asserted_hours + 0.5:
            out.append(Discrepancy(
                "timeliness_claim_contradicted",
                f"{a.doc_id} claims notice within {a.asserted_hours:g}h, but the timestamps show {measured:g}h "
                f"({start.kind} {start.ts} -> {end.kind} {end.ts}).",
                sorted({a.doc_id, start.doc_id, end.doc_id}), True,
                "Timestamped records outrank narrative assertions, so the measured interval is used."))

    # 2. Amounts that disagree with the claim in the system of record
    if facts.survey_amount is not None and case.claim_amount and abs(facts.survey_amount - case.claim_amount) > 0.01 * case.claim_amount:
        lower = facts.survey_amount < case.claim_amount
        out.append(Discrepancy(
            "amount_mismatch",
            f"Claimed amount {case.claim_amount:,.0f} {case.currency} differs from the independent survey estimate "
            f"{facts.survey_amount:,.0f} {case.currency}.",
            [d.doc_id for d in case.doc("survey")], lower,
            "The settlement basis is limited to the lower evidenced loss." if lower else "The claimed amount is used; the survey estimate is higher."))
    if facts.form_amount is not None and case.claim_amount and abs(facts.form_amount - case.claim_amount) > 0.01 * case.claim_amount:
        out.append(Discrepancy(
            "claim_form_mismatch",
            f"The claim form states {facts.form_amount:,.0f} but the system of record has {case.claim_amount:,.0f}.",
            [d.doc_id for d in case.doc("claim_form")], True, "Confirm the correct amount with the claimant before settlement."))

    # 3. Two sources disagree on when the port closed
    closures = facts.all("closure_effective")
    if len({e.ts for e in closures}) > 1:
        official = next((e for e in closures if e.attr("source") == "port_notice"), closures[0])
        out.append(Discrepancy("conflicting_timestamps",
                               "Sources give different closure times: " + "; ".join(f"{e.doc_id} says {e.ts}" for e in closures) + ".",
                               sorted({e.doc_id for e in closures}), True,
                               f"The official port notice ({official.ts}) outranks other sources."))

    # 4. The same event recorded at different times
    for kind in ("insurer_notice", "carrier_notice", "hold_notice", "submission_complete"):
        evs = facts.all(kind)
        if len({e.ts for e in evs}) > 1:
            out.append(Discrepancy("conflicting_timestamps", f"{kind} is recorded at different times: " + ", ".join(e.ts for e in evs) + ".",
                                   sorted({e.doc_id for e in evs}), True, "Needs human confirmation of the correct time."))
    return out


# ---- fact assembly ------------------------------------------------------------------------------------------

def build_facts(case: CaseRecord, extra_events: list[Event] | None = None) -> CaseFacts:
    events, assertions = extract_events(case)
    events.extend(extra_events or [])
    facts = CaseFacts(case_id=case.case_id, events=events, assertions=assertions)

    for doc in case.documents:
        if doc.kind == "survey" and doc.readable:
            m = re.search(r"Estimated loss[^.]*?USD\s*([\d,]+)", doc.text, re.I)
            if m:
                facts.survey_amount = float(m.group(1).replace(",", ""))
        if doc.kind == "claim_form" and doc.readable:
            m = re.search(r"Claimed amount:\s*USD\s*([\d,]+)", doc.text, re.I)
            if m:
                facts.form_amount = float(m.group(1).replace(",", ""))

    first_notice = facts.first("carrier_notice")
    if first_notice and case.case_type == "freight_damage":
        facts.events.append(Event(first_notice.ts, "glcc_discovery", "system", first_notice.doc_id,
                                  "assumption: GLCC discovers the loss when the carrier first notifies it"))

    for kind, label in REQUIRED_KINDS.get(case.case_type, []):
        if not any(d.kind == kind and d.readable for d in case.documents):
            facts.missing_documents.append(label)
    if case.case_type == "customs_hold" and not facts.first("hold_notice"):
        facts.missing_documents.append("hold_notice")

    facts.unreadable_documents = [d.doc_id for d in case.documents if not d.readable]
    for d in case.documents:
        if d.readable:
            for hit in injection_matches(d.text):
                facts.security_flags.append({"doc_id": d.doc_id, "match": hit})
    facts.missing_required_filings = sorted({e.attr("filing") for e in facts.events if e.kind == "missing_document"})

    if case.case_type == "customs_hold":
        facts.fault = "glcc" if facts.first("admission_glcc_error") else "unknown"
    vb = facts.first("verbal_notice")
    wn = facts.first("written_notice")
    if vb and wn and vb.ts < wn.ts:
        facts.notes.append(f"Verbal notice at {vb.ts} ({vb.doc_id}) does not satisfy a written-notice requirement; written notice came at {wn.ts}.")

    facts.discrepancies = find_discrepancies(case, facts)
    return facts
