"""Deterministic resolution of conflicting contract clauses.

When several clauses address the same topic, exactly one may govern. The rules run in a fixed order
and every elimination records its reason, so a reviewer can see why a clause lost:

  0. Different vendor           the clause belongs to another carrier's agreement
  1. Not applicable             the cargo shipped before the clause applies (applies_from > shipment date)
  2. Explicit supersession      another applicable clause says it "replaces Section X of <doc>"
  3. Specificity                a vendor-specific clause overrides a general one
                                (MSA clause 1.2 states this order of precedence in the contract itself)
  4. Recency                    the later effective date prevails
  5. Tie                        more than one clause remains: NOT resolved, escalate to a human

Why code and not an LLM: the outcome moves money. It must be reproducible, testable against known
contract structures, and explainable line by line. An LLM may help parse unfamiliar contracts into
this structure; it does not decide which clause wins.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..models import Clause

RULES = [
    "0. Clauses from another vendor's agreement are excluded.",
    "1. Clauses that do not yet apply on the shipment date are excluded.",
    "2. A clause explicitly replaced by another applicable clause is overridden.",
    "3. A vendor-specific clause overrides a general clause on the same topic.",
    "4. Among the remaining clauses, the later effective date prevails.",
    "5. If more than one clause still remains, the conflict is unresolved and goes to a human.",
]


@dataclass
class TopicRuling:
    topic: str
    candidates: list[str]
    governing: Clause | None
    overridden: list[dict] = field(default_factory=list)       # {clause_id, reason}
    unresolved: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)

    @property
    def needs_human(self) -> bool:
        return self.governing is None

    def to_dict(self) -> dict:
        return {
            "topic": self.topic, "candidates": self.candidates,
            "governing_clause": self.governing.clause_id if self.governing else None,
            "governing_terms": self.governing.terms if self.governing else {},
            "governing_text": self.governing.text if self.governing else None,
            "overridden": self.overridden, "unresolved": self.unresolved,
            "needs_human": self.needs_human, "steps": self.steps,
        }


def resolve_topic(topic: str, clauses: list[Clause], shipment_date: str, vendor: str) -> TopicRuling:
    ruling = TopicRuling(topic=topic, candidates=[c.clause_id for c in clauses], governing=None)
    remaining = list(clauses)

    def eliminate(c: Clause, reason: str) -> None:
        ruling.overridden.append({"clause_id": c.clause_id, "reason": reason})
        remaining.remove(c)

    for c in list(remaining):                                                  # rule 0
        if c.vendor not in ("ALL", vendor):
            eliminate(c, f"belongs to {c.vendor}'s agreement, not {vendor}'s")
    for c in list(remaining):                                                  # rule 1
        if c.applies_from > shipment_date:
            eliminate(c, f"applies only to cargo shipped on or after {c.applies_from}; this cargo shipped {shipment_date}")
    for c in list(remaining):                                                  # rule 2
        replacers = [o.clause_id for o in remaining if c.clause_id in o.supersedes]
        if replacers:
            eliminate(c, f"explicitly replaced by {replacers[0]}")
    if len(remaining) > 1 and any(c.scope == "vendor_specific" for c in remaining):   # rule 3
        winners = [c.clause_id for c in remaining if c.scope == "vendor_specific"]
        for c in list(remaining):
            if c.scope == "general":
                eliminate(c, f"general clause overridden by vendor-specific {winners[0]}")
    if len(remaining) > 1:                                                     # rule 4
        latest = max(c.effective_date for c in remaining)
        for c in list(remaining):
            if c.effective_date != latest:
                eliminate(c, f"older effective date ({c.effective_date}) than {latest}")

    if len(remaining) == 1:
        ruling.governing = remaining[0]
        ruling.steps.append(f"{topic}: {len(clauses)} candidate(s) -> {remaining[0].clause_id} governs "
                            f"({len(ruling.overridden)} overridden)")
    elif len(remaining) == 0:
        ruling.steps.append(f"{topic}: no applicable clause remains")
    else:
        ruling.unresolved = sorted(c.clause_id for c in remaining)
        ruling.steps.append(f"{topic}: UNRESOLVED tie between {', '.join(ruling.unresolved)} "
                            "(same scope, same effective date, neither replaces the other)")
    return ruling


def resolve(clauses: list[Clause], shipment_date: str, vendor: str) -> dict[str, TopicRuling]:
    """Resolve every topic present in `clauses`. Clauses with topic 'other' are ignored."""
    by_topic: dict[str, list[Clause]] = defaultdict(list)
    for c in {c.clause_id: c for c in clauses}.values():        # de-duplicate by identity (clause_id)
        if c.topic != "other":
            by_topic[c.topic].append(c)
    return {topic: resolve_topic(topic, cs, shipment_date, vendor) for topic, cs in sorted(by_topic.items())}
