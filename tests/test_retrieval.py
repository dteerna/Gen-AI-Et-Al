import unittest
from pathlib import Path

from glcc.corpus import Corpus
from glcc.retrieval.evaluation import RetrievalEvaluator, load_queries, score_query
from glcc.retrieval.index import BM25, HybridIndex, expand_query, stem, tokenize, trigrams
from glcc.retrieval.retrievers import ClauseRetriever, RegulationRetriever, RulingRetriever

DATA = Path(__file__).resolve().parent.parent / "data"
CORPUS = Corpus.load(DATA)
CLAUSES = ClauseRetriever(CORPUS)
REGS = RegulationRetriever(CORPUS)
RULINGS = RulingRetriever(CORPUS)


def ids(hits):
    return [h.item.clause_id for h in hits]


class TestTextProcessing(unittest.TestCase):
    def test_stemming_unifies_forms(self):
        self.assertEqual(stem("damaged"), stem("damages"))
        self.assertEqual(stem("liabilities"), stem("liability"))
        self.assertEqual(stem("hours"), stem("hour"))

    def test_short_tokens_untouched(self):
        self.assertEqual(stem("usd"), "usd")

    def test_tokenize_drops_stopwords_and_punctuation(self):
        self.assertEqual(tokenize("The limit, of liability!"), ["limit", "liability"])

    def test_expansion_only_adds_known_synonyms(self):
        self.assertIn("limit", expand_query("liability cap"))
        self.assertEqual(expand_query("xyzzy"), "xyzzy")

    def test_trigrams_survive_a_typo(self):
        a, b = trigrams("liability"), trigrams("liablity")
        shared = sum((a & b).values())
        self.assertGreater(shared / sum(a.values()), 0.5)


class TestRankers(unittest.TestCase):
    def test_bm25_prefers_rarer_terms(self):
        docs = [tokenize("carrier carrier carrier"), tokenize("carrier liability"), tokenize("unrelated words here")]
        scores = BM25(docs).scores(tokenize("liability carrier"))
        self.assertGreater(scores[1], scores[0])
        self.assertEqual(scores[2], 0.0)

    def test_hybrid_is_deterministic(self):
        a = ids(CLAUSES.search("liability limit cargo damage", vendor="ACME Freight", k=5))
        b = ids(CLAUSES.search("liability limit cargo damage", vendor="ACME Freight", k=5))
        self.assertEqual(a, b)

    def test_empty_and_stopword_only_queries_return_nothing(self):
        for q in ("", "   ", "the of and"):
            self.assertEqual(CLAUSES.search(q), [], repr(q))

    def test_gibberish_returns_nothing_or_little(self):
        self.assertEqual(CLAUSES.search("qqqzzzxxx"), [])

    def test_k_is_respected(self):
        self.assertLessEqual(len(CLAUSES.search("liability", k=2)), 2)

    def test_typo_found_by_hybrid_but_not_by_bm25_alone(self):
        q = "forse mjeure"          # every word genuinely misspelled, so BM25 has nothing to match
        self.assertEqual(CLAUSES.search(q, k=3, mode="bm25"), [])
        self.assertEqual(ids(CLAUSES.search(q, k=3, mode="hybrid"))[0], "MSA-GLCC-001#5.1")

    def test_ranks_and_provenance_present(self):
        hits = CLAUSES.search("force majeure written notice", k=3)
        self.assertEqual([h.rank for h in hits], [1, 2, 3][: len(hits)])
        self.assertIsNotNone(hits[0].bm25_rank)
        self.assertIn("score", hits[0].provenance(hits[0].item.clause_id))

    def test_hybrid_index_works_with_a_filter_that_excludes_everything(self):
        idx = HybridIndex(["alpha beta", "gamma"], lambda x: x)
        self.assertEqual(idx.search("alpha", keep=lambda x: False), [])


class TestClauseFilters(unittest.TestCase):
    def test_vendor_filter_excludes_other_vendors_clauses(self):
        got = set(ids(CLAUSES.search("liability limit cargo", vendor="Zenith Cargo", k=10)))
        self.assertTrue(got)
        self.assertFalse({c for c in got if c.startswith(("VA-ACME", "VA-NORTHSTAR", "ADD-NORTHSTAR"))})
        self.assertIn("MSA-GLCC-001#3.1", got)           # general clauses apply to every vendor

    def test_vendor_filter_includes_vendor_specific(self):
        got = set(ids(CLAUSES.search("liability limit cargo", vendor="ACME Freight", k=10)))
        self.assertTrue({"VA-ACME-2022#5.1", "VA-ACME-2022-AMD2#1.1", "MSA-GLCC-001#3.1"} <= got)

    def test_topic_filter(self):
        hits = CLAUSES.search("hours", vendor="ACME Freight", topic="notification_deadline", k=10)
        self.assertTrue(hits)
        self.assertTrue(all(h.item.topic == "notification_deadline" for h in hits))

    def test_distractor_is_retrievable_when_it_is_the_right_answer(self):
        self.assertEqual(ids(CLAUSES.search("audit access notice hours", k=1)), ["MSA-GLCC-001#4.2"])

    def test_superseded_clauses_are_still_returned_for_explainability(self):
        self.assertIn("VA-ACME-2022#5.1", ids(CLAUSES.search("liability limit", vendor="ACME Freight", topic="liability_cap", k=10)))

    def test_topics_available(self):
        topics = CLAUSES.topics_available("Orion Shipping")
        self.assertIn("liability_cap", topics)
        self.assertNotIn("other", topics)


class TestTemporalRegulations(unittest.TestCase):
    Q = "customs hold submission deadline business days"

    def test_version_in_force_on_the_incident_date_is_returned(self):
        new = REGS.search(self.Q, port="Northgate", as_of="2025-02-03").hits
        old = REGS.search(self.Q, port="Northgate", as_of="2024-11-04").hits
        self.assertEqual(new[0].item.section_id, "PAN-REG-003.v2#2")
        self.assertEqual(old[0].item.section_id, "PAN-REG-003.v1#2")

    def test_a_superseded_version_can_never_be_returned(self):
        for as_of in ("2025-02-03", "2026-01-01"):
            got = {h.item.section_id for h in REGS.search(self.Q, port="Northgate", as_of=as_of, k=10).hits}
            self.assertFalse({g for g in got if g.startswith("PAN-REG-003.v1")}, as_of)

    def test_exclusions_are_reported_with_reasons(self):
        res = REGS.search(self.Q, port="Northgate", as_of="2025-02-03")
        reasons = {e["id"]: e["reason"] for e in res.excluded}
        self.assertIn("PAN-REG-003.v1#2", reasons)
        self.assertIn("not in force on 2025-02-03", reasons["PAN-REG-003.v1#2"])

    def test_before_any_version_existed_nothing_applies(self):
        self.assertEqual(REGS.search(self.Q, port="Northgate", as_of="2020-01-01").hits, [])

    def test_boundary_dates(self):
        self.assertEqual(REGS.search(self.Q, port="Northgate", as_of="2025-01-14").hits[0].item.version, 1)
        self.assertEqual(REGS.search(self.Q, port="Northgate", as_of="2025-01-15").hits[0].item.version, 2)

    def test_port_filter(self):
        hits = REGS.search("customs hold required documents", port="Southhaven", as_of="2024-06-01").hits
        self.assertTrue(hits)
        self.assertTrue(all(h.item.port == "Southhaven" for h in hits))
        self.assertEqual(REGS.search(self.Q, port="Atlantis", as_of="2025-02-03").hits, [])


class TestRulings(unittest.TestCase):
    def test_finds_the_relevant_precedent(self):
        top = RULINGS.search("force majeure late written notice port closure", case_type="vessel_delay")[0]
        self.assertEqual(top.item.ruling_id, "RULING-2023-014")

    def test_case_type_is_a_hard_filter(self):
        for h in RULINGS.search("claim notice limit charges", case_type="customs_hold", k=5):
            self.assertEqual(h.item.case_type, "customs_hold")

    def test_no_filler_precedents_for_unrelated_queries(self):
        self.assertEqual(RULINGS.search("xyzzy plugh", case_type="freight_damage"), [])

    def test_same_vendor_is_noted(self):
        hits = RULINGS.search("freight damage liability cap", case_type="freight_damage", vendor="ACME Freight", k=5)
        self.assertTrue(any("same vendor" in h.notes for h in hits))


class TestMetrics(unittest.TestCase):
    def test_perfect(self):
        s = score_query(["a", "b", "c"], ["a", "b"], 3)
        self.assertEqual((s["recall"], s["rr"], round(s["ndcg"], 6)), (1.0, 1.0, 1.0))

    def test_partial_and_rank(self):
        s = score_query(["x", "a", "y"], ["a", "b"], 3)
        self.assertEqual(s["recall"], 0.5)
        self.assertEqual(s["rr"], 0.5)
        self.assertEqual(s["first_hit_rank"], 2)
        self.assertLess(s["ndcg"], 1.0)

    def test_miss(self):
        s = score_query(["x", "y"], ["a"], 2)
        self.assertEqual((s["recall"], s["rr"], s["ndcg"], s["first_hit_rank"]), (0.0, 0.0, 0.0, None))

    def test_recall_cannot_exceed_k_over_relevant(self):
        self.assertEqual(score_query(["a", "b", "c"], ["a", "b", "c", "d"], 3)["recall"], 0.75)


class TestQualityGate(unittest.TestCase):
    """Regression gate: fails if retrieval quality drops below the level documented in reports/."""

    @classmethod
    def setUpClass(cls):
        cls.queries = load_queries(DATA / "retrieval_eval.json")
        cls.k5 = RetrievalEvaluator(CORPUS).run(cls.queries, k=5)

    def test_labeled_set_is_nontrivial(self):
        self.assertGreaterEqual(len(self.queries), 25)
        tags = {t for q in self.queries for t in q["tags"]}
        self.assertTrue({"typo", "hard_negative", "temporal", "paraphrase", "port_filter"} <= tags)

    def test_hybrid_quality_floor(self):
        h = self.k5["overall"]["hybrid"]
        self.assertGreaterEqual(h["recall@5"], 0.95)
        self.assertGreaterEqual(h["mrr"], 0.90)

    def test_hybrid_never_worse_than_bm25_on_typos(self):
        row = self.k5["by_tag"]["typo"]
        self.assertGreaterEqual(row["hybrid"]["recall@5"], row["bm25"]["recall@5"])

    def test_temporal_queries_are_exactly_right(self):
        row = self.k5["by_tag"]["temporal"]["hybrid"]
        self.assertEqual((row["recall@5"], row["mrr"]), (1.0, 1.0))

    def test_every_relevant_id_exists_in_the_corpus(self):
        known = CORPUS.known_ids()
        universe = known["clause"] | known["regulation"] | known["ruling"]
        for q in self.queries:
            for rid in q["relevant"]:
                self.assertIn(rid, universe, q["id"])


if __name__ == "__main__":
    unittest.main()
