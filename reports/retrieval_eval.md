# Retrieval evaluation

Labeled queries: 26. Metrics: Recall@k, MRR, nDCG@k with binary relevance.

## Overall, k = 3

| Mode | Recall@3 | MRR | nDCG@3 |
|---|---|---|---|
| bm25 | 0.965 | 0.974 | 0.957 |
| trigram | 0.971 | 1.000 | 0.985 |
| hybrid | 0.971 | 0.981 | 0.971 |

## Overall, k = 5

| Mode | Recall@5 | MRR | nDCG@5 |
|---|---|---|---|
| bm25 | 0.987 | 0.974 | 0.965 |
| trigram | 1.000 | 1.000 | 0.995 |
| hybrid | 1.000 | 0.981 | 0.981 |

## By query type, k = 5

| Tag | n | bm25 R@5 | trigram R@5 | hybrid R@5 | hybrid MRR |
|---|---|---|---|---|---|
| exact | 14 | 1.00 | 1.00 | 1.00 | 0.96 |
| hard_negative | 2 | 1.00 | 1.00 | 1.00 | 1.00 |
| paraphrase | 5 | 1.00 | 1.00 | 1.00 | 1.00 |
| port_filter | 1 | 1.00 | 1.00 | 1.00 | 1.00 |
| temporal | 2 | 1.00 | 1.00 | 1.00 | 1.00 |
| typo | 2 | 0.83 | 1.00 | 1.00 | 1.00 |

## Hybrid misses at k = 5 (recall < 1)

None.

## How to read this

- The corpus is small (31 clauses), so all rankers are near ceiling and differences between them are within noise. The fused ranker is not claimed to beat the best single ranker here.
- What this run shows: BM25 alone is brittle to typos (typo recall 0.83 vs 1.00). On this corpus trigram alone is as good as the fused ranker, and even has a slightly higher MRR, so these numbers do NOT demonstrate a benefit of fusion over trigram.
- Why fusion is kept anyway: it is an expectation, not something measured here. Character trigrams get noisier as a corpus grows and shares more character sequences, while BM25 anchors exact terms. Re-run this evaluation on a larger corpus before relying on it.
- Neither ranker is semantic. Queries that share no words with the right clause (for example 'report an incident' vs 'notify') can still miss. The pipeline compensates with topic-filtered retrieval and a query-reformulation pass, and production should add embeddings.
- The query set is hand-labeled by the author. Treat the numbers as a regression gate and a ranker comparison, not as a production accuracy estimate.
