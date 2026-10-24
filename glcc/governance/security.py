"""Defenses against untrusted content: injection screening and PII redaction.

Case documents are written by third parties, so they are DATA, never instructions. The offline
pipeline cannot be "persuaded" by text (it only matches patterns), but documents still get scanned so
that an attempted manipulation is flagged, recorded in the audit trail, and raises the review level.
When an LLM is used, documents are additionally wrapped as untrusted data and redacted first.
"""

from __future__ import annotations

import re
from typing import Any

_INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) (instructions|prompts?)",
    r"disregard (the |all )?(above|previous|prior)",
    r"reveal (your |the )?(system )?(prompt|instructions)",
    r"\bsystem prompt\b",
    r"\bsystem instruction\b",
    r"instruction to (the )?(ai|assistant|model)",
    r"approve (the )?(payout|payment|claim|settlement)( in full)?( immediately| now)?",
    r"without (any )?review",
    r"do not mention this",
    r"you are now\b",
    r"act as (an? )?(unrestricted|jailbroken)",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def looks_like_injection(text: str) -> bool:
    """Cheap heuristic. It only catches obvious attempts; the approval gate is the real backstop."""
    return bool(text and _INJECTION_RE.search(text))


def injection_matches(text: str) -> list[str]:
    return [m.group(0) for m in _INJECTION_RE.finditer(text or "")]


def scan_for_injection(obj: Any) -> list[str]:
    hits: list[str] = []
    if isinstance(obj, str):
        hits.extend(injection_matches(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            hits.extend(scan_for_injection(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            hits.extend(scan_for_injection(v))
    return hits


_REDACTIONS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED-SSN]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[REDACTED-EMAIL]"),
    (re.compile(r"(?<![\d-])(\+?\d{1,3}[\s-]?)?(\(?\d{3}\)?[\s-]?)\d{3}[\s-]?\d{4}(?![\d-])"), "[REDACTED-PHONE]"),
]


def redact_text(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj
