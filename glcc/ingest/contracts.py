"""Parse contract PDFs into clause-level records with machine-readable terms.

Chunking is clause-aware: one retrieval unit per numbered clause, never a fixed-size window that
could split a limit from its condition. Each clause keeps its document id, clause number, page,
effective date, applicability date, and any clause it explicitly replaces.

Topic classification and term extraction here are deterministic rules. In production, an LLM can
propose them for unfamiliar layouts, but the rules are the baseline and are unit-tested.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import Clause
from .common import IngestError, normalize_ws, parse_date, pdf_pages, valid_iso_date
from .vendors import VendorRegistry

_HEADER_KEYS = {
    "document id": "doc_id", "type": "doc_type", "vendor": "vendor", "effective date": "effective",
    "amends": "amends", "applies to cargo shipped on or after": "applies_from",
}
_DOC_TYPES = {
    "master agreement": "master_agreement", "vendor agreement": "vendor_agreement", "amendment": "amendment",
    "addendum": "addendum", "insurance policy": "insurance_policy",
}
_VENDOR_SPECIFIC = {"vendor_agreement", "amendment", "addendum"}

_CLAUSE_START = re.compile(r"^(\d{1,2}(?:\.\d{1,2})+)\s+([A-Z].*)$")
_ARTICLE = re.compile(r"^ARTICLE\s+\d+\b")
_FOOTER = re.compile(r"^Page\s+\d+(\s+of\s+\d+)?$")
_SUPERSEDES = re.compile(r"(?:replaces|supersedes)\s+(?:Section|Clause)\s+(\d+(?:\.\d+)*)\s+of\s+([A-Z0-9][A-Z0-9\-]+)", re.I)
_SHIPPED_ON_OR_AFTER = re.compile(r"shipped\s+on\s+or\s+after\s+(.+?)(?:[.,;]|$)", re.I)


def _amount(s: str) -> float:
    return float(s.replace(",", ""))


def classify_topic(text: str) -> str:
    """Ordered keyword rules. First match wins. Unknown clauses are 'other' and never enter conflict resolution.

    Order matters: a notice clause often mentions "detention" ("notify ... of any damage, detention, or
    delay within 48 hours"), so notification is tested before detention, and detention requires a
    charges-allocation context.
    """
    t = text.lower()
    if "force majeure" in t:
        return "force_majeure"
    if "insur" in t and ("deductible" in t or "coverage" in t or "insured loss" in t):
        return "insurance_coverage"
    if ("notify" in t or "notification" in t) and "hours" in t:
        return "notification_deadline"
    if ("detention" in t or "demurrage" in t) and ("charges" in t or "borne" in t):
        return "detention_demurrage"
    if "liabilit" in t and "usd" in t and ("limited to" in t or "limit" in t):
        return "liability_cap"
    if "customs" in t and "document" in t:
        return "customs_responsibility"
    return "other"


def extract_terms(topic: str, text: str) -> dict:
    """Machine-readable terms for a topic. Returns {} when the text has no recognizable terms."""
    t = text
    terms: dict = {}
    if topic == "liability_cap":
        m = re.search(r"USD\s*([\d,]+)", t)
        per = re.search(r"per\s+(container|shipment)", t, re.I)
        if m:
            terms["liability_cap"] = _amount(m.group(1))
        if per:
            terms["per"] = per.group(1).lower()
    elif topic == "notification_deadline":
        m = re.search(r"within\s+(\d+)\s+hours", t, re.I)
        if m:
            terms["notification_hours"] = int(m.group(1))
    elif topic == "force_majeure":
        m = re.search(r"written\s+notice\s+within\s+(\d+)\s+hours", t, re.I)
        if m:
            terms["written_notice_hours"] = int(m.group(1))
        terms["verbal_notice_sufficient"] = not re.search(r"verbal notice does not", t, re.I)
    elif topic == "insurance_coverage":
        for key, pat in (("deductible", r"USD\s*([\d,]+)\s+deductible"), ("coverage_percent", r"at\s+(\d+)%\s+of\s+the\s+insured\s+loss"),
                         ("late_notice_coverage_percent", r"reduced\s+to\s+(\d+)%"), ("insurer_notice_hours", r"within\s+(\d+)\s+hours\s+of\s+GLCC")):
            m = re.search(pat, t, re.I)
            if m:
                terms[key] = int(m.group(1)) if key.endswith("hours") else _amount(m.group(1))
    elif topic == "detention_demurrage":
        if re.search(r"carrier-caused[^.]*borne by the carrier", t, re.I):
            terms["carrier_caused"] = "carrier"
        if re.search(r"documentation errors?[^.]*borne by GLCC", t, re.I):
            terms["documentation_error"] = "glcc"
    return terms


def _parse_header(lines: list[str]) -> dict:
    meta: dict = {}
    for ln in lines:
        m = re.match(r"^([A-Za-z ]+):\s*(.+)$", ln)
        if m and m.group(1).strip().lower() in _HEADER_KEYS:
            meta[_HEADER_KEYS[m.group(1).strip().lower()]] = m.group(2).strip()
    return meta


def parse_contract(path: Path, vendors: VendorRegistry) -> list[Clause]:
    """Parse one contract PDF. Raises IngestError / UnreadableDocument for unusable files."""
    pages = pdf_pages(path)
    page_lines = [[ln.strip() for ln in p.splitlines() if ln.strip() and not _FOOTER.match(ln.strip())] for p in pages]

    # Header: everything before the first ARTICLE, across the first page
    header_lines: list[str] = []
    for ln in page_lines[0]:
        if _ARTICLE.match(ln):
            break
        header_lines.append(ln)
    meta = _parse_header(header_lines)
    for required in ("doc_id", "doc_type", "effective"):
        if required not in meta:
            raise IngestError(f"{path.name}: missing header field '{required}'")
    if not valid_iso_date(meta["effective"]):
        raise IngestError(f"{path.name}: invalid effective date {meta['effective']!r}")

    doc_type = _DOC_TYPES.get(meta["doc_type"].strip().lower())
    if doc_type is None:
        raise IngestError(f"{path.name}: unknown document type {meta['doc_type']!r}")
    raw_vendor = meta.get("vendor", "ALL")
    vendor = "ALL" if raw_vendor.upper() == "ALL" else vendors.canonical(raw_vendor)
    if vendor is None:
        raise IngestError(f"{path.name}: unknown vendor {raw_vendor!r}; add it to vendors.json")
    doc_applies_from = meta.get("applies_from") or meta["effective"]
    if not valid_iso_date(doc_applies_from):
        raise IngestError(f"{path.name}: invalid applicability date {doc_applies_from!r}")

    # Clauses: a numbered line starts a clause, following unnumbered lines continue it
    blocks: list[dict] = []
    seen_article = False
    for pno, lines in enumerate(page_lines, start=1):
        for ln in lines:
            if _ARTICLE.match(ln):
                seen_article = True
                continue
            if not seen_article:
                continue
            m = _CLAUSE_START.match(ln)
            if m:
                blocks.append({"number": m.group(1), "text": m.group(2), "page": pno})
            elif blocks:
                blocks[-1]["text"] += " " + ln

    clauses = []
    for b in blocks:
        text = normalize_ws(b["text"])
        topic = classify_topic(text)
        supersedes = tuple(f"{doc.upper()}#{num}" for num, doc in _SUPERSEDES.findall(text))
        applies_from = doc_applies_from
        m = _SHIPPED_ON_OR_AFTER.search(text)
        if m and parse_date(m.group(1)):
            applies_from = parse_date(m.group(1))
        heading = text.split(". ")[0] if len(text.split(". ")[0]) <= 60 else text[:50]
        clauses.append(Clause(
            clause_id=f"{meta['doc_id']}#{b['number']}", doc_id=meta["doc_id"], doc_type=doc_type, vendor=vendor,
            scope="vendor_specific" if doc_type in _VENDOR_SPECIFIC else "general", topic=topic, number=b["number"],
            heading=heading, text=text, effective_date=meta["effective"], applies_from=applies_from,
            supersedes=supersedes, terms=extract_terms(topic, text), page=b["page"], source=path.name))
    if not clauses:
        raise IngestError(f"{path.name}: no numbered clauses found")
    return clauses
