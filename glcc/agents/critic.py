"""The reviewer. It trusts nothing it is shown and checks the memo against the analysis and the corpus.

Checks:
  1. every cited id exists in the corpus AND was part of the evidence for this case;
  2. every money-like number in the text appears in the analysis (an invented figure fails);
  3. the decision, the required reviewers, and the "no action taken" sentence are present;
  4. every blocker, discrepancy, and security flag in the analysis is acknowledged;
  5. nothing claims an action was already carried out.

Used twice: on any LLM-written narrative before it is accepted, and on the final memo before a proposal exists.
"""

from __future__ import annotations

import json
import re

from .memo import SENTENCE

ID_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*(?:\.v\d+)?#\d+(?:\.\d+)*\b|\bRULING-\d{4}-\d{3}\b")
NUM_RE = re.compile(r"(?<![\w.#-])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.#-])\d{4,}(?:\.\d+)?(?![\w-])")
EXECUTED_RE = re.compile(r"\b(?:has|have|had|was|were|is)\s+(?:already\s+|now\s+)?(?:been\s+)?(?:paid|executed|released|submitted|wired|transferred|cleared)\b", re.I)


def cited_ids(text: str) -> set[str]:
    return set(ID_RE.findall(text or ""))


def _norm_number(s: str) -> str:
    s = s.replace(",", "")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def numbers_in(text: str) -> set[str]:
    return {_norm_number(n) for n in NUM_RE.findall(text or "")}


def allowed_numbers(analysis: dict) -> set[str]:
    blob = json.dumps(analysis, default=str)
    return {_norm_number(n) for n in re.findall(r"\d[\d,]*\.?\d*", blob) if len(n.replace(",", "").split(".")[0]) >= 4 or "," in n}


def allowed_ids(analysis: dict) -> set[str]:
    ev = analysis["evidence"]
    ids = set(ev["clauses"]) | set(ev["regulations"]) | set(ev["rulings"]) | {e["id"] for e in ev["regulations_excluded"]}
    return ids | {p["ruling_id"] for p in analysis["precedents"]}


def narrative_sections(memo: str) -> str:
    """The parts of a memo that a model may have written: the Summary and Explanation sections."""
    parts = re.findall(r"^## (?:Summary|Explanation)\n(.*?)(?=^## |\Z)", memo or "", re.S | re.M)
    return "\n".join(parts)


def check_text(text: str, analysis: dict, known: dict[str, set[str]], scan_executed: bool = True) -> list[str]:
    """Checks that apply to any text shown to a reviewer (a narrative or a whole memo).

    scan_executed is False for a whole memo, because its Precedents section legitimately says things like
    "a claim was paid" about HISTORICAL rulings. For a memo, the executed-claim scan is applied to the
    narrative sections only (see review_memo).
    """
    issues: list[str] = []
    universe = known["clause"] | known["regulation"] | known["ruling"]
    for cid in sorted(cited_ids(text)):
        if cid not in universe:
            issues.append(f"cites {cid}, which does not exist in the corpus")
        elif cid not in allowed_ids(analysis):
            issues.append(f"cites {cid}, which is not part of the evidence for this case")
    extra = numbers_in(text) - allowed_numbers(analysis)
    if extra:
        issues.append(f"contains figure(s) not found in the analysis: {', '.join(sorted(extra))}")
    # The mandatory disclaimer legitimately contains the word "executed" ("before anything is executed"); scan everything else.
    m = EXECUTED_RE.search((text or "").replace(SENTENCE, "")) if scan_executed else None
    if m:
        issues.append(f"claims an action was already carried out: {m.group(0)!r}")
    return issues


def review_memo(memo: str, analysis: dict, known: dict[str, set[str]]) -> list[str]:
    """Full review of the final memo. An empty list means it may become a proposal."""
    issues = check_text(memo, analysis, known, scan_executed=False)
    issues += [i for i in check_text(narrative_sections(memo), analysis, known) if "already carried out" in i]
    d = analysis["decision"]
    if SENTENCE not in memo:
        issues.append("missing the mandatory 'no action has been taken' statement")
    if f"`{d['decision_type']}`" not in memo:
        issues.append("does not state the decision type")
    if d["decision_type"] in ("approve_settlement", "partial_settlement") and f"{d['amount']:,.2f}" not in memo:
        issues.append("does not state the settlement amount")
    for role in d["required_approvals"]:
        if role not in memo:
            issues.append(f"does not name the required reviewer {role}")
    for b in analysis["blockers"]:
        if b["kind"] not in memo:
            issues.append(f"omits blocker {b['kind']}")
    for disc in analysis.get("discrepancies", []) or []:
        if disc["kind"] not in memo:
            issues.append(f"omits discrepancy {disc['kind']}")
    if "injection_suspected" in analysis["flags"] and "SECURITY" not in memo:
        issues.append("omits the security finding")
    return issues
