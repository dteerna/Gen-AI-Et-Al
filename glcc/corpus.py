"""The knowledge base: contracts, regulations, rulings, vendors, and cases.

Loading is fault tolerant. A broken or unreadable document is recorded and skipped; it does not
stop the system. Unreadable vendor agreements matter: the pipeline refuses to assume the master
agreement applies when a vendor-specific agreement exists but could not be read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .ingest.cases import load_case
from .ingest.common import IngestError, UnreadableDocument
from .ingest.contracts import parse_contract
from .ingest.regulations import apply_validity_windows, parse_regulation
from .ingest.rulings import load_rulings
from .ingest.vendors import VendorRegistry
from .models import CaseRecord, Clause, RegSection, Ruling


@dataclass
class Corpus:
    root: Path
    vendors: VendorRegistry
    clauses: list[Clause] = field(default_factory=list)
    reg_sections: list[RegSection] = field(default_factory=list)
    rulings: list[Ruling] = field(default_factory=list)
    unreadable_documents: list[dict] = field(default_factory=list)   # {doc_id, vendor, reason}
    warnings: list[str] = field(default_factory=list)

    # ---- construction -------------------------------------------------------------------------
    @classmethod
    def load(cls, root: str | Path) -> "Corpus":
        root = Path(root)
        if not root.is_dir():
            raise IngestError(f"data directory not found: {root}")
        corpus = cls(root=root, vendors=VendorRegistry.load(root / "vendors.json"))

        for pdf in sorted((root / "contracts").glob("*.pdf")):
            try:
                corpus.clauses.extend(parse_contract(pdf, corpus.vendors))
            except UnreadableDocument as exc:
                corpus.unreadable_documents.append(
                    {"doc_id": pdf.stem, "vendor": corpus._vendor_from_filename(pdf.stem), "reason": str(exc)})
                corpus.warnings.append(f"unreadable contract skipped: {exc}")
            except IngestError as exc:
                corpus.warnings.append(f"contract skipped: {exc}")

        sections: list[RegSection] = []
        for txt in sorted((root / "regulations").glob("*.txt")):
            try:
                sections.extend(parse_regulation(txt))
            except IngestError as exc:
                corpus.warnings.append(f"regulation skipped: {exc}")
        corpus.reg_sections = apply_validity_windows(sections)

        rulings_path = root / "rulings" / "rulings.jsonl"
        if rulings_path.exists():
            try:
                corpus.rulings = load_rulings(rulings_path)
            except IngestError as exc:
                corpus.warnings.append(f"rulings skipped: {exc}")

        # Duplicate ids would make citations ambiguous; keep the first and warn.
        seen: set[str] = set()
        unique = []
        for c in corpus.clauses:
            if c.clause_id in seen:
                corpus.warnings.append(f"duplicate clause id ignored: {c.clause_id}")
                continue
            seen.add(c.clause_id)
            unique.append(c)
        corpus.clauses = unique
        return corpus

    def _vendor_from_filename(self, stem: str) -> str | None:
        """Vendor agreements follow the naming convention VA-<VENDORKEY>-<year>."""
        m = re.match(r"^(?:VA|ADD|AMD)-([A-Z0-9]+)-", stem.upper())
        return self.vendors.by_key(m.group(1)) if m else None

    # ---- lookups -------------------------------------------------------------------------------------
    def case_ids(self) -> list[str]:
        return sorted(p.name for p in (self.root / "cases").iterdir() if (p / "case.json").exists())

    def case(self, case_id: str) -> CaseRecord:
        folder = self.root / "cases" / case_id
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", case_id or "") or not (folder / "case.json").exists():
            raise IngestError(f"unknown case {case_id!r}")
        return load_case(folder, self.vendors)

    def known_ids(self) -> dict[str, set[str]]:
        return {
            "clause": {c.clause_id for c in self.clauses},
            "regulation": {s.section_id for s in self.reg_sections},
            "ruling": {r.ruling_id for r in self.rulings},
        }

    def clause(self, clause_id: str) -> Clause | None:
        return next((c for c in self.clauses if c.clause_id == clause_id), None)

    def reg_section(self, section_id: str) -> RegSection | None:
        return next((s for s in self.reg_sections if s.section_id == section_id), None)

    def unreadable_for(self, vendor: str) -> list[dict]:
        return [u for u in self.unreadable_documents if u["vendor"] == vendor]

    def stats(self) -> dict:
        return {"clauses": len(self.clauses), "documents": len({c.doc_id for c in self.clauses}),
                "regulation_sections": len(self.reg_sections), "rulings": len(self.rulings),
                "unreadable_documents": len(self.unreadable_documents), "warnings": len(self.warnings)}
