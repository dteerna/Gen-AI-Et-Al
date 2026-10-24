"""Domain retrievers built on the hybrid index.

Metadata filters are applied BEFORE ranking, so a clause for the wrong vendor or a regulation
version that was not in force can never be returned. Everything excluded is reported, so the
audit trail shows what was considered and why it was left out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..corpus import Corpus
from ..models import Clause, RegSection, Ruling
from .index import Hit, HybridIndex, expand_query


def _clause_text(c: Clause) -> str:
    return f"{c.topic.replace('_', ' ')} {c.doc_type.replace('_', ' ')} {c.vendor} {c.text}"


def _reg_text(s: RegSection) -> str:
    return f"{s.topic.replace('_', ' ')} {s.authority} {s.port} {s.text}"


def _ruling_text(r: Ruling) -> str:
    return f"{r.case_type.replace('_', ' ')} {r.vendor} {r.summary} {r.rationale} {r.outcome.replace('_', ' ')}"


@dataclass
class RegulationResult:
    hits: list[Hit[RegSection]]
    excluded: list[dict] = field(default_factory=list)      # matches that were not in force on the as-of date


class ClauseRetriever:
    def __init__(self, corpus: Corpus):
        self.corpus = corpus
        self.index = HybridIndex(corpus.clauses, _clause_text)

    def search(self, query: str, *, vendor: str = "any", topic: str | None = None, k: int = 5,
               mode: str = "hybrid", reformulate: bool = False) -> list[Hit[Clause]]:
        q = expand_query(query) if reformulate else query
        wanted = (vendor or "any").strip()

        def keep(c: Clause) -> bool:
            if topic and c.topic != topic:
                return False
            return wanted.lower() in ("any", "all", "") or c.vendor in ("ALL", wanted)

        return self.index.search(q, keep=keep, k=k, mode=mode)

    def topics_available(self, vendor: str) -> set[str]:
        return {c.topic for c in self.corpus.clauses if c.vendor in ("ALL", vendor) and c.topic != "other"}


class RegulationRetriever:
    def __init__(self, corpus: Corpus):
        self.corpus = corpus
        self.index = HybridIndex(corpus.reg_sections, _reg_text)

    def search(self, query: str, *, port: str, as_of: str, k: int = 3, mode: str = "hybrid", topic: str | None = None) -> RegulationResult:
        want_port = (port or "any").strip().lower()

        def port_ok(s: RegSection) -> bool:
            return (want_port in ("any", "all", "") or s.port.lower() == want_port) and (topic is None or s.topic == topic)

        hits = self.index.search(query, keep=lambda s: port_ok(s) and s.in_force(as_of), k=k, mode=mode)
        wide = self.index.search(query, keep=port_ok, k=k * 3, mode=mode)
        in_force_ids = {h.item.section_id for h in hits}
        excluded = [
            {"id": h.item.section_id, "reason": f"not in force on {as_of} (valid {h.item.effective_date} to {h.item.valid_until or 'present'})"}
            for h in wide if h.item.section_id not in in_force_ids and not h.item.in_force(as_of)
        ]
        return RegulationResult(hits=hits, excluded=excluded)


class RulingRetriever:
    def __init__(self, corpus: Corpus):
        self.corpus = corpus
        self.index = HybridIndex(corpus.rulings, _ruling_text)

    def search(self, query: str, *, case_type: str | None = None, vendor: str | None = None, k: int = 3,
               mode: str = "hybrid") -> list[Hit[Ruling]]:
        hits = self.index.search(query, keep=lambda r: not case_type or r.case_type == case_type, k=k * 2, mode=mode)
        for h in hits:
            if vendor and h.item.vendor in (vendor, "ALL"):
                h.score *= 1.15
                h.notes.append("same vendor")
        hits.sort(key=lambda h: -h.score)
        for rank, h in enumerate(hits[:k], start=1):
            h.rank = rank
        return [h for h in hits[:k] if h.bm25_rank is not None]      # require real lexical overlap, no filler precedents


@dataclass
class Retrievers:
    """The three retrievers, built once and shared read-only across runs and threads."""
    clauses: ClauseRetriever
    regulations: RegulationRetriever
    rulings: RulingRetriever

    @classmethod
    def build(cls, corpus: Corpus) -> "Retrievers":
        return cls(ClauseRetriever(corpus), RegulationRetriever(corpus), RulingRetriever(corpus))
