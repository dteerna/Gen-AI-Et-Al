"""Shared ingestion helpers: errors, date parsing, and PDF text extraction."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


class IngestError(Exception):
    """A document could not be parsed."""


class UnreadableDocument(IngestError):
    """The document has no extractable text (for example a scanned image). It needs OCR."""


_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"], start=1)}


def parse_date(text: str) -> str | None:
    """Parse '2024-03-01' or '1 March 2024' into ISO form. Returns None if no date is found."""
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        return m.group(0)
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b", text)
    if m and m.group(2).lower() in _MONTHS:
        return f"{int(m.group(3)):04d}-{_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return None


def valid_iso_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def pdf_pages(path: Path) -> list[str]:
    """Extract text per page. Raises UnreadableDocument for image-only PDFs."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:                       # pragma: no cover - environment dependent
        raise IngestError("Reading PDFs needs the 'pypdf' package: pip install pypdf") from exc
    try:
        reader = PdfReader(str(path))
        pages = [(p.extract_text() or "") for p in reader.pages]
    except Exception as exc:
        raise IngestError(f"Could not open PDF {path.name}: {exc}") from exc
    if not "".join(pages).strip():
        raise UnreadableDocument(f"{path.name} has no text layer (scanned image?). OCR is required.")
    return pages
