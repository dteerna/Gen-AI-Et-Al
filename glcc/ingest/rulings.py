"""Load historical exception rulings from JSON Lines."""

from __future__ import annotations

import json
from pathlib import Path

from ..models import Ruling
from .common import IngestError

_REQUIRED = ("ruling_id", "date", "case_type", "vendor", "summary", "outcome", "clauses_applied", "rationale")


def load_rulings(path: Path) -> list[Ruling]:
    out = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IngestError(f"{Path(path).name} line {n}: invalid JSON ({exc.msg})") from exc
        missing = [k for k in _REQUIRED if k not in rec]
        if missing:
            raise IngestError(f"{Path(path).name} line {n}: missing {missing}")
        out.append(Ruling(ruling_id=rec["ruling_id"], date=rec["date"], case_type=rec["case_type"], vendor=rec["vendor"],
                          summary=rec["summary"], outcome=rec["outcome"], clauses_applied=tuple(rec["clauses_applied"]),
                          rationale=rec["rationale"]))
    return out
