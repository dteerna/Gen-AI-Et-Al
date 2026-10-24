"""Load a case folder: a structured system-of-record entry (case.json) plus messy documents.

Supported inputs: .eml emails, chat logs, survey/notice text files, PDF attachments, and images.
Images and scanned PDFs are marked unreadable: this offline build has no OCR, and the pipeline
treats that as missing evidence instead of silently ignoring the file. In the Azure build, plug an
OCR or vision step (Azure AI Document Intelligence, or a vision model) into `read_image`.
"""

from __future__ import annotations

import json
from email import policy as email_policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from datetime import timezone
from pathlib import Path

from ..models import CaseRecord, Document
from .common import IngestError, UnreadableDocument, pdf_pages, valid_iso_date
from .vendors import VendorRegistry

CASE_TYPES = {"freight_damage", "customs_hold", "vessel_delay"}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}


def read_image(path: Path) -> str | None:
    """Extension point for OCR / vision. Returns text, or None when no reader is configured."""
    return None


def _utc_minute(header_value: str) -> str:
    dt = parsedate_to_datetime(header_value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M")


def _parse_email(path: Path) -> Document:
    msg = BytesParser(policy=email_policy.default).parsebytes(path.read_bytes())
    body_part = msg.get_body(preferencelist=("plain",))
    body = body_part.get_content() if body_part is not None else ""
    warnings = []
    try:
        date = _utc_minute(msg["Date"])
    except Exception:
        date = ""
        warnings.append("email has no parseable Date header; its events cannot be timestamped")
    meta = {"from": str(msg["From"] or ""), "to": str(msg["To"] or ""), "date": date, "subject": str(msg["Subject"] or "")}
    return Document(doc_id=path.stem, kind="email", filename=path.name, text=body.strip(), meta=meta, warnings=warnings)


def _kind_for_text(stem: str) -> str:
    s = stem.lower()
    for prefix, kind in (("chat", "chat"), ("survey", "survey"), ("notice", "notice")):
        if s.startswith(prefix):
            return kind
    return "other"


def load_documents(case_dir: Path) -> list[Document]:
    docs: list[Document] = []
    for path in sorted(p for p in case_dir.iterdir() if p.is_file() and p.name != "case.json"):
        ext = path.suffix.lower()
        try:
            if ext == ".eml":
                docs.append(_parse_email(path))
            elif ext == ".txt":
                docs.append(Document(path.stem, _kind_for_text(path.stem), path.name, path.read_text(encoding="utf-8")))
            elif ext == ".pdf":
                try:
                    text = "\n".join(pdf_pages(path))
                    docs.append(Document(path.stem, "claim_form" if path.stem.startswith("attachment") else "other", path.name, text))
                except UnreadableDocument as exc:
                    docs.append(Document(path.stem, "image", path.name, "", readable=False, warnings=[str(exc)]))
            elif ext in _IMAGE_EXT:
                text = read_image(path)
                if text:
                    docs.append(Document(path.stem, "image", path.name, text))
                else:
                    docs.append(Document(path.stem, "image", path.name, "", readable=False,
                                         warnings=["image not analyzed: no OCR/vision reader configured"]))
            else:
                docs.append(Document(path.stem, "other", path.name, "", readable=False, warnings=[f"unsupported file type {ext}"]))
        except IngestError as exc:
            docs.append(Document(path.stem, "other", path.name, "", readable=False, warnings=[str(exc)]))
        except Exception as exc:                       # a corrupt file must not take the whole case down
            docs.append(Document(path.stem, "other", path.name, "", readable=False, warnings=[f"failed to read: {exc}"]))
    return docs


def load_case(case_dir: Path, vendors: VendorRegistry) -> CaseRecord:
    case_dir = Path(case_dir)
    try:
        rec = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise IngestError(f"{case_dir.name}: case.json not found") from exc
    except json.JSONDecodeError as exc:
        raise IngestError(f"{case_dir.name}: case.json is not valid JSON ({exc.msg})") from exc

    for req in ("case_id", "case_type", "vendor", "shipment_date", "port", "claim_amount"):
        if req not in rec:
            raise IngestError(f"{case_dir.name}: case.json missing '{req}'")
    if rec["case_type"] not in CASE_TYPES:
        raise IngestError(f"{rec['case_id']}: unknown case_type {rec['case_type']!r}")
    if not valid_iso_date(rec["shipment_date"]):
        raise IngestError(f"{rec['case_id']}: invalid shipment_date {rec['shipment_date']!r}")
    try:
        amount = float(rec["claim_amount"])
    except (TypeError, ValueError) as exc:
        raise IngestError(f"{rec['case_id']}: claim_amount must be a number") from exc
    if amount < 0:
        raise IngestError(f"{rec['case_id']}: claim_amount cannot be negative")

    canonical = vendors.canonical(rec["vendor"])
    return CaseRecord(
        case_id=rec["case_id"], case_type=rec["case_type"], vendor=canonical or rec["vendor"], vendor_raw=rec["vendor"],
        shipment_date=rec["shipment_date"], port=rec["port"], claim_amount=amount, currency=rec.get("currency", "USD"),
        containers=max(1, int(rec.get("containers", 1))), detention_charge_per_day=float(rec.get("detention_charge_per_day", 0)),
        documents=load_documents(case_dir))
