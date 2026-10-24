"""Typed data models shared by every layer. Plain dataclasses, no third-party dependencies."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


def to_jsonable(obj: Any) -> Any:
    """Convert dataclasses, tuples, and sets to JSON-friendly structures."""
    return json.loads(json.dumps(asdict(obj) if hasattr(obj, "__dataclass_fields__") else obj,
                                 default=lambda o: sorted(o) if isinstance(o, set) else list(o)))


# ---- knowledge base ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Clause:
    clause_id: str                 # "VA-ACME-2022#5.1": document id + clause number, as printed in the PDF
    doc_id: str
    doc_type: str                  # master_agreement | vendor_agreement | amendment | addendum | insurance_policy
    vendor: str                    # canonical vendor name, or "ALL"
    scope: str                     # general | vendor_specific
    topic: str                     # liability_cap | notification_deadline | force_majeure | ... | other
    number: str
    heading: str
    text: str
    effective_date: str            # ISO date the document took effect
    applies_from: str              # ISO date: applies to cargo shipped on or after this date
    supersedes: tuple[str, ...]    # clause ids this clause explicitly replaces
    terms: dict                    # machine-readable terms parsed from the text
    page: int
    source: str

    def to_dict(self) -> dict:
        return to_jsonable(self)


@dataclass(frozen=True)
class RegSection:
    section_id: str                # "PAN-REG-003.v2#2"
    reg_id: str
    version: int
    authority: str
    port: str
    topic: str
    number: str
    heading: str
    text: str
    effective_date: str
    valid_until: str | None        # exclusive; set when a later version supersedes this one
    supersedes: str | None
    terms: dict
    source: str

    def in_force(self, as_of: str) -> bool:
        return self.effective_date <= as_of and (self.valid_until is None or as_of < self.valid_until)

    def to_dict(self) -> dict:
        return to_jsonable(self)


@dataclass(frozen=True)
class Ruling:
    ruling_id: str
    date: str
    case_type: str
    vendor: str
    summary: str
    outcome: str
    clauses_applied: tuple[str, ...]
    rationale: str

    def to_dict(self) -> dict:
        return to_jsonable(self)


# ---- case inputs ---------------------------------------------------------------------------------

@dataclass
class Document:
    doc_id: str                    # file stem, e.g. "email_01_carrier_notice"
    kind: str                      # email | chat | survey | notice | claim_form | image | other
    filename: str
    text: str
    meta: dict = field(default_factory=dict)      # for emails: from, to, date, subject
    readable: bool = True
    warnings: list[str] = field(default_factory=list)


@dataclass
class CaseRecord:
    case_id: str
    case_type: str                 # freight_damage | customs_hold | vessel_delay
    vendor: str                    # canonical vendor name
    vendor_raw: str
    shipment_date: str
    port: str
    claim_amount: float
    currency: str
    containers: int
    detention_charge_per_day: float
    documents: list[Document]

    def doc(self, kind: str) -> list[Document]:
        return [d for d in self.documents if d.kind == kind]


# ---- extracted facts -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    ts: str                        # "YYYY-MM-DD HH:MM", UTC
    kind: str                      # see reasoning/timeline.py EVENT_KINDS
    actor: str
    doc_id: str
    quote: str
    attrs: tuple[tuple[str, str], ...] = ()

    def attr(self, key: str, default: str | None = None) -> str | None:
        return dict(self.attrs).get(key, default)

    def to_dict(self) -> dict:
        return {"ts": self.ts, "kind": self.kind, "actor": self.actor, "doc_id": self.doc_id,
                "quote": self.quote, "attrs": dict(self.attrs)}


@dataclass(frozen=True)
class Assertion:
    doc_id: str
    speaker: str
    kind: str                      # timeliness_claim
    asserted_hours: float
    quote: str

    def to_dict(self) -> dict:
        return to_jsonable(self)


@dataclass
class Discrepancy:
    kind: str
    description: str
    doc_ids: list[str]
    material: bool                 # would it change a compliance or monetary result?
    resolution: str                # how the evidence hierarchy resolved it

    def to_dict(self) -> dict:
        return to_jsonable(self)


@dataclass
class CaseFacts:
    case_id: str
    events: list[Event] = field(default_factory=list)
    assertions: list[Assertion] = field(default_factory=list)
    discrepancies: list[Discrepancy] = field(default_factory=list)
    survey_amount: float | None = None
    form_amount: float | None = None
    missing_documents: list[str] = field(default_factory=list)       # required document kinds absent from the file
    missing_required_filings: list[str] = field(default_factory=list)  # e.g. customs documents the port says are missing
    unreadable_documents: list[str] = field(default_factory=list)
    security_flags: list[dict] = field(default_factory=list)
    fault: str | None = None       # glcc | carrier | unknown (customs holds)
    notes: list[str] = field(default_factory=list)

    def first(self, kind: str) -> Event | None:
        cands = sorted((e for e in self.events if e.kind == kind), key=lambda e: e.ts)
        return cands[0] if cands else None

    def all(self, kind: str) -> list[Event]:
        return sorted((e for e in self.events if e.kind == kind), key=lambda e: e.ts)

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "events": [e.to_dict() for e in sorted(self.events, key=lambda e: e.ts)],
            "assertions": [a.to_dict() for a in self.assertions],
            "discrepancies": [d.to_dict() for d in self.discrepancies],
            "survey_amount": self.survey_amount, "form_amount": self.form_amount,
            "missing_documents": self.missing_documents,
            "missing_required_filings": self.missing_required_filings,
            "unreadable_documents": self.unreadable_documents,
            "security_flags": self.security_flags, "fault": self.fault, "notes": self.notes,
        }
