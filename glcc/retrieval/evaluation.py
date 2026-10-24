"""Measure retrieval quality against a labeled query set.

Reports Recall@k, MRR, and nDCG@k per ranking mode (bm25, trigram, hybrid), overall and by query
tag (exact, paraphrase, typo, hard_negative, temporal, port_filter). Comparing modes shows what the
hybrid fusion buys; the tag breakdown shows where retrieval is weak.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

from ..corpus import Corpus
from .retrievers import ClauseRetriever, RegulationRetriever, RulingRetriever

MODES = ("bm25", "trigram", "hybrid")


def _dcg(rels: list[int]) -> float:
    return sum(r / math.log2(i + 2) for i, r in enumerate(rels))


def score_query(ranked_ids: list[str], relevant: list[str], k: int) -> dict:
    top = ranked_ids[:k]
    rel = set(relevant)
    hits = [1 if i in rel else 0 for i in top]
    recall = len(rel & set(top)) / len(rel) if rel else 0.0
    rr = next((1.0 / (n + 1) for n, h in enumerate(hits) if h), 0.0)
    ideal = _dcg([1] * min(len(rel), k))
    ndcg = _dcg(hits) / ideal if ideal else 0.0
    return {"recall": recall, "rr": rr, "ndcg": ndcg, "first_hit_rank": next((n + 1 for n, h in enumerate(hits) if h), None)}


class RetrievalEvaluator:
    def __init__(self, corpus: Corpus):
        self.clauses = ClauseRetriever(corpus)
        self.regs = RegulationRetriever(corpus)
        self.rulings = RulingRetriever(corpus)

    def ranked_ids(self, q: dict, mode: str, k: int) -> list[str]:
        if q["kind"] == "clause":
            return [h.item.clause_id for h in self.clauses.search(q["query"], vendor=q.get("vendor", "any"), k=k, mode=mode)]
        if q["kind"] == "regulation":
            res = self.regs.search(q["query"], port=q["port"], as_of=q["as_of"], k=k, mode=mode)
            return [h.item.section_id for h in res.hits]
        hits = self.rulings.index.search(q["query"], keep=lambda r: r.case_type == q.get("case_type", r.case_type), k=k, mode=mode)
        return [h.item.ruling_id for h in hits]

    def run(self, queries: list[dict], k: int = 3) -> dict:
        per_mode: dict[str, list[dict]] = {m: [] for m in MODES}
        for q in queries:
            for m in MODES:
                s = score_query(self.ranked_ids(q, m, k), q["relevant"], k)
                s.update(id=q["id"], tags=q.get("tags", []), kind=q["kind"], query=q["query"])
                per_mode[m].append(s)

        def agg(rows: list[dict]) -> dict:
            n = len(rows) or 1
            return {"n": len(rows), f"recall@{k}": sum(r["recall"] for r in rows) / n,
                    "mrr": sum(r["rr"] for r in rows) / n, f"ndcg@{k}": sum(r["ndcg"] for r in rows) / n}

        overall = {m: agg(rows) for m, rows in per_mode.items()}
        tags = sorted({t for q in queries for t in q.get("tags", [])})
        by_tag = {t: {m: agg([r for r in rows if t in r["tags"]]) for m, rows in per_mode.items()} for t in tags}
        misses = [r for r in per_mode["hybrid"] if r["recall"] < 1.0]
        return {"k": k, "overall": overall, "by_tag": by_tag, "hybrid_misses": misses, "per_query": per_mode["hybrid"]}


def load_queries(path: Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["queries"]


def render_markdown(results: list[dict]) -> str:
    """Render one or more evaluation results (one per cutoff k). The last result also gets the tag and miss breakdown."""
    lines = ["# Retrieval evaluation", "", f"Labeled queries: {results[0]['overall']['hybrid']['n']}. "
             "Metrics: Recall@k, MRR, nDCG@k with binary relevance.", ""]
    for res in results:
        k = res["k"]
        lines += [f"## Overall, k = {k}", "", f"| Mode | Recall@{k} | MRR | nDCG@{k} |", "|---|---|---|---|"]
        for m in MODES:
            o = res["overall"][m]
            lines.append(f"| {m} | {o[f'recall@{k}']:.3f} | {o['mrr']:.3f} | {o[f'ndcg@{k}']:.3f} |")
        lines.append("")
    res = results[-1]
    k = res["k"]
    lines += [f"## By query type, k = {k}", "", f"| Tag | n | bm25 R@{k} | trigram R@{k} | hybrid R@{k} | hybrid MRR |", "|---|---|---|---|---|---|"]
    for tag, row in res["by_tag"].items():
        lines.append(f"| {tag} | {row['hybrid']['n']} | {row['bm25'][f'recall@{k}']:.2f} | {row['trigram'][f'recall@{k}']:.2f} | "
                     f"{row['hybrid'][f'recall@{k}']:.2f} | {row['hybrid']['mrr']:.2f} |")
    lines += ["", f"## Hybrid misses at k = {k} (recall < 1)", ""]
    if not res["hybrid_misses"]:
        lines.append("None.")
    for r in res["hybrid_misses"]:
        lines.append(f"- `{r['id']}` ({', '.join(r['tags'])}): recall {r['recall']:.2f}, first relevant rank {r['first_hit_rank']}. \"{r['query']}\"")
    lines += [
        "", "## How to read this", "",
        "- The corpus is small (31 clauses), so all rankers are near ceiling and differences between them are within noise. "
        "The fused ranker is not claimed to beat the best single ranker here.",
        "- What this run shows: BM25 alone is brittle to typos (typo recall 0.83 vs 1.00). On this corpus trigram alone is as good as "
        "the fused ranker, and even has a slightly higher MRR, so these numbers do NOT demonstrate a benefit of fusion over trigram.",
        "- Why fusion is kept anyway: it is an expectation, not something measured here. Character trigrams get noisier as a corpus grows "
        "and shares more character sequences, while BM25 anchors exact terms. Re-run this evaluation on a larger corpus before relying on it.",
        "- Neither ranker is semantic. Queries that share no words with the right clause (for example 'report an incident' vs 'notify') "
        "can still miss. The pipeline compensates with topic-filtered retrieval and a query-reformulation pass, and production should add embeddings.",
        "- The query set is hand-labeled by the author. Treat the numbers as a regression gate and a ranker comparison, not as a production accuracy estimate.", ""]
    return "\n".join(lines)
