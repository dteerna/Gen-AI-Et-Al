"""A small, dependency-free hybrid retrieval engine.

Two complementary rankers, fused with Reciprocal Rank Fusion (RRF):
  * BM25 over stemmed word tokens: precise on exact terms ("48 hours", "force majeure").
  * Character-trigram TF-IDF cosine: robust to typos and morphology ("liablity", "forse majeur").

This is lexical hybrid retrieval, not semantic. It has no embeddings, so it will not match pure
paraphrases that share no words. The `Retriever` interface is deliberately narrow so Azure AI
Search (keyword + vector + semantic ranking) can replace it; see docs/DESIGN_DECISIONS.md.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar

T = TypeVar("T")

STOPWORDS = {
    "a", "an", "the", "of", "to", "in", "on", "for", "and", "or", "is", "are", "was", "be", "by", "with", "that",
    "this", "it", "as", "at", "from", "how", "what", "do", "does", "should", "if", "any", "per", "not", "than",
    "who", "when", "which", "there", "their", "its", "must", "can", "may", "will",
}

# Used only for query REFORMULATION when a first retrieval pass finds nothing for a required topic.
SYNONYMS = {
    "cap": ["limit", "limitation", "maximum", "ceiling"],
    "limit": ["cap", "limitation", "maximum"],
    "damage": ["loss", "damaged"],
    "notice": ["notify", "notification", "inform"],
    "notify": ["notice", "notification", "inform"],
    "delay": ["late", "delayed"],
    "closure": ["closed", "closing"],
    "insurance": ["insurer", "insured", "coverage"],
    "customs": ["port", "clearance"],
    "hold": ["detained", "detention"],
}


def stem(tok: str) -> str:
    if len(tok) <= 3:
        return tok
    if tok.endswith("ies") and len(tok) > 4:
        return tok[:-3] + "y"
    for suf in ("ations", "ation", "ings", "ing", "ed", "es", "s", "e"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[: -len(suf)]
    return tok


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [stem(w) for w in words if w not in STOPWORDS and len(w) > 1]


def expand_query(query: str) -> str:
    extra: list[str] = []
    for w in re.findall(r"[a-z]+", query.lower()):
        extra.extend(SYNONYMS.get(w, []))
    return query + " " + " ".join(dict.fromkeys(extra)) if extra else query


def trigrams(text: str) -> Counter:
    t = " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip() + " "
    return Counter(t[i:i + 3] for i in range(len(t) - 2))


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(d) for d in docs]
        self.len = [len(d) for d in docs]
        self.avg = (sum(self.len) / len(docs)) if docs else 0.0
        df: Counter = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        out = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for t in set(query):
                if t in tf:
                    denom = tf[t] + self.k1 * (1 - self.b + self.b * self.len[i] / (self.avg or 1))
                    s += self.idf[t] * tf[t] * (self.k1 + 1) / denom
            out.append(s)
        return out


class TrigramIndex:
    def __init__(self, texts: list[str]):
        grams = [trigrams(t) for t in texts]
        df: Counter = Counter()
        for g in grams:
            df.update(g.keys())
        n = len(texts)
        self.idf = {t: math.log(1 + n / f) for t, f in df.items()}
        self.vecs = [self._vec(g) for g in grams]

    def _vec(self, g: Counter) -> dict[str, float]:
        v = {t: c * self.idf.get(t, 0.0) for t, c in g.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    def scores(self, query: str) -> list[float]:
        q = self._vec(trigrams(query))
        return [sum(w * v.get(t, 0.0) for t, w in q.items()) for v in self.vecs]


@dataclass
class Hit(Generic[T]):
    item: T
    score: float                      # fused RRF score (or the single ranker's score in single-mode)
    rank: int
    bm25_rank: int | None = None
    trigram_rank: int | None = None
    notes: list[str] = field(default_factory=list)

    def provenance(self, item_id: str) -> dict:
        return {"id": item_id, "score": round(self.score, 5), "rank": self.rank,
                "bm25_rank": self.bm25_rank, "trigram_rank": self.trigram_rank}


class HybridIndex(Generic[T]):
    RRF_K = 60
    TRIGRAM_MIN = 0.12                # cosine floor: below this a trigram match is noise

    def __init__(self, items: list[T], text_of: Callable[[T], str]):
        self.items = items
        texts = [text_of(i) for i in items]
        self._bm25 = BM25([tokenize(t) for t in texts])
        self._tri = TrigramIndex(texts)

    def search(self, query: str, *, keep: Callable[[T], bool] | None = None, k: int = 5, mode: str = "hybrid") -> list[Hit[T]]:
        allowed = [i for i, it in enumerate(self.items) if keep is None or keep(it)]
        # A query with no content words ("the of and") must return nothing. Without this guard the trigram
        # ranker would happily match arbitrary text on shared character sequences.
        if not allowed or not tokenize(query):
            return []
        bm = self._bm25.scores(tokenize(query))
        tr = self._tri.scores(query)

        def ranked(scores: list[float], floor: float) -> list[int]:
            cands = [i for i in allowed if scores[i] > floor]
            return sorted(cands, key=lambda i: (-scores[i], i))

        bm_rank = {i: r for r, i in enumerate(ranked(bm, 0.0), start=1)}
        tr_rank = {i: r for r, i in enumerate(ranked(tr, self.TRIGRAM_MIN), start=1)}

        fused: dict[int, float] = {}
        if mode in ("hybrid", "bm25"):
            for i, r in bm_rank.items():
                fused[i] = fused.get(i, 0.0) + 1.0 / (self.RRF_K + r)
        if mode in ("hybrid", "trigram"):
            for i, r in tr_rank.items():
                fused[i] = fused.get(i, 0.0) + 1.0 / (self.RRF_K + r)

        order = sorted(fused, key=lambda i: (-fused[i], i))[:k]
        return [Hit(self.items[i], fused[i], rank, bm_rank.get(i), tr_rank.get(i)) for rank, i in enumerate(order, start=1)]
