"""Parse port-authority regulation text files into section-level records with version windows."""

from __future__ import annotations

import re
from pathlib import Path

from ..models import RegSection
from .common import IngestError, normalize_ws, valid_iso_date

_KNOWN_DOCS = ("commercial invoice", "certificate of origin", "packing list", "bill of lading")
_HEADER = re.compile(r"^(Regulation ID|Version|Authority|Port|Topic|Effective Date|Supersedes):\s*(.+)$")
_SECTION = re.compile(r"^Section\s+(\d+)\.\s+(.*)$")


def extract_reg_terms(text: str) -> dict:
    terms: dict = {}
    m = re.search(r"submitted\s+within\s+(\d+)\s+business\s+days", text, re.I)
    if m:
        terms["submission_business_days"] = int(m.group(1))
    m = re.search(r"released\s+within\s+(\d+)\s+business\s+days", text, re.I)
    if m:
        terms["release_business_days"] = int(m.group(1))
    docs = [d for d in _KNOWN_DOCS if d in text.lower()]
    if docs:
        terms["required_documents"] = docs
    return terms


def parse_regulation(path: Path) -> list[RegSection]:
    meta: dict[str, str] = {}
    sections: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        ln = raw.strip()
        if not ln:
            continue
        m = _HEADER.match(ln)
        if m and not sections:
            meta[m.group(1)] = m.group(2).strip()
            continue
        m = _SECTION.match(ln)
        if m:
            sections.append({"number": m.group(1), "text": m.group(2)})
        elif sections:
            sections[-1]["text"] += " " + ln
    for req in ("Regulation ID", "Version", "Authority", "Port", "Topic", "Effective Date"):
        if req not in meta:
            raise IngestError(f"{path.name}: missing header field '{req}'")
    if not valid_iso_date(meta["Effective Date"]):
        raise IngestError(f"{path.name}: invalid effective date {meta['Effective Date']!r}")
    if not sections:
        raise IngestError(f"{path.name}: no sections found")
    version = int(meta["Version"])
    out = []
    for s in sections:
        text = normalize_ws(s["text"])
        heading = text.split(". ")[0]
        out.append(RegSection(
            section_id=f"{meta['Regulation ID']}.v{version}#{s['number']}", reg_id=meta["Regulation ID"], version=version,
            authority=meta["Authority"], port=meta["Port"], topic=meta["Topic"], number=s["number"], heading=heading,
            text=text, effective_date=meta["Effective Date"], valid_until=None, supersedes=meta.get("Supersedes"),
            terms=extract_reg_terms(text), source=path.name))
    return out


def apply_validity_windows(sections: list[RegSection]) -> list[RegSection]:
    """Close each version's validity at the effective date of the next version of the same regulation."""
    from dataclasses import replace

    versions: dict[str, dict[int, str]] = {}
    for s in sections:
        versions.setdefault(s.reg_id, {})[s.version] = s.effective_date

    def valid_until(reg_id: str, version: int) -> str | None:
        later = sorted(v for v in versions[reg_id] if v > version)
        return versions[reg_id][later[0]] if later else None

    return [replace(s, valid_until=valid_until(s.reg_id, s.version)) for s in sections]
