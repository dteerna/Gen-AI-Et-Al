import json
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from glcc.agents import critic
from glcc.agents.analysis_agents import AnalystAgent
from glcc.agents.base import Agent, RunContext
from glcc.agents.drafting import CriticAgent, DrafterAgent
from glcc.agents.intake import IntakeAgent
from glcc.agents.memo import SENTENCE, render_memo
from glcc.agents.orchestrator import PIPELINE_SERIAL_1, PIPELINE_SERIAL_2, RESEARCHERS, Orchestrator
from glcc.agents.research import ClauseResearcher, PrecedentResearcher, RegulationResearcher, expand_by_supersession
from glcc.corpus import Corpus
from glcc.governance.store import Store
from glcc.llm.base import LLMError, ResilientLLM
from glcc.llm.fake import MODES, ScriptedLLM, SimulatedLLM
from glcc.proposals import build_proposal, escalation_analysis
from glcc.retrieval.retrievers import ClauseRetriever

DATA = Path(__file__).resolve().parent.parent / "data"
C = Corpus.load(DATA)

GOLDEN = {
    "CASE-1001": ("partial_settlement", 89000.0, ["underwriter"], []),
    "CASE-1002": ("clear_customs_hold", 0.0, ["compliance_officer"], []),
    "CASE-1003": ("approve_settlement", 40000.0, ["underwriter", "legal_counsel"], ["material_discrepancy"]),
    "CASE-1004": ("approve_settlement", 30000.0, ["underwriter", "legal_counsel"], ["injection_suspected"]),
    "CASE-1005": ("escalate_to_legal", 0.0, ["legal_counsel"], []),
    "CASE-1006": ("partial_settlement", 142500.0, ["senior_underwriter", "legal_counsel"], ["material_discrepancy"]),
    "CASE-1007": ("request_information", 0.0, ["logistics_coordinator"], []),
    "CASE-1008": ("approve_settlement", 90000.0, ["underwriter"], []),
    "CASE-1009": ("approve_settlement", 95000.0, ["underwriter"], []),
    "CASE-1010": ("clear_customs_hold", 0.0, ["compliance_officer"], []),
    "CASE-1011": ("request_information", 0.0, ["logistics_coordinator"], []),
}
RUNS: dict[str, dict] = {}


def run(cid):
    if cid not in RUNS:
        RUNS[cid] = Orchestrator(C).run(cid)
    return RUNS[cid]


def blockers(r):
    return {b["kind"] for b in r["analysis"]["blockers"]}


def no_sleep_llm(inner, **kw):
    return ResilientLLM(inner, sleep=lambda s: None, **kw)


class TestGoldenOutcomes(unittest.TestCase):
    def test_every_sample_case_has_the_expected_outcome(self):
        self.assertEqual(set(GOLDEN), set(C.case_ids()))
        for cid, (decision, amount, approvals, flags) in GOLDEN.items():
            r = run(cid)
            self.assertEqual(r["status"], "completed", (cid, r["error"]))
            self.assertEqual((r["decision"], r["amount"]), (decision, amount), cid)
            c = r["proposal"]["content"]
            self.assertEqual((c["required_approvals"], c["flags"]), (approvals, flags), cid)

    def test_amendment_overrides_older_caps(self):
        a = run("CASE-1001")["analysis"]
        g = a["governing"]["liability_cap"]
        self.assertEqual(g["governing_clause"], "VA-ACME-2022-AMD2#1.1")
        self.assertEqual({o["clause_id"] for o in g["overridden"]}, {"VA-ACME-2022#5.1", "MSA-GLCC-001#3.1"})
        self.assertEqual((a["payout"]["vendor_liability"], a["payout"]["insurance_payable"], a["payout"]["unrecovered"]), (80000.0, 9000.0, 6000.0))

    def test_cargo_shipped_before_the_amendment_keeps_the_old_cap(self):
        a = run("CASE-1009")["analysis"]
        self.assertEqual(a["governing"]["liability_cap"]["governing_clause"], "VA-ACME-2022#5.1")
        self.assertIn("applies only to cargo shipped on or after 2024-03-01",
                      {o["clause_id"]: o["reason"] for o in a["governing"]["liability_cap"]["overridden"]}["VA-ACME-2022-AMD2#1.1"])

    def test_vendor_without_an_agreement_uses_the_master_per_container_cap(self):
        a = run("CASE-1008")["analysis"]
        self.assertEqual(a["payout"]["cap"], {"cap_per_unit": 50000.0, "per": "container", "containers": 2, "cap_total": 100000.0,
                                              "note": "50,000 per container x 2 container(s)"})

    def test_customs_hold_uses_the_regulation_version_in_force(self):
        new, old = run("CASE-1002")["analysis"], run("CASE-1010")["analysis"]
        self.assertEqual(new["evidence"]["regulations"], ["PAN-REG-003.v2#1", "PAN-REG-003.v2#2", "PAN-REG-003.v2#3"])
        self.assertEqual(old["evidence"]["regulations"], ["PAN-REG-003.v1#1", "PAN-REG-003.v1#2", "PAN-REG-003.v1#3"])
        self.assertIn("PAN-REG-003.v1#2", {e["id"] for e in new["evidence"]["regulations_excluded"]})
        self.assertIn("PAN-REG-003.v2#2", {e["id"] for e in old["evidence"]["regulations_excluded"]})
        self.assertEqual((new["customs"]["detention_charges"], new["customs"]["responsible_party"]), (1200.0, "glcc"))
        self.assertEqual(old["customs"]["detention_charges"], 7200.0)

    def test_conflicting_facts_are_resolved_by_timestamps_and_flagged(self):
        r = run("CASE-1003")
        self.assertEqual([d["kind"] for d in r["analysis"]["discrepancies"]], ["timeliness_claim_contradicted"])
        self.assertIn("RULING-2023-014", r["analysis"]["evidence"]["rulings"])
        self.assertFalse(r["analysis"]["checks"]["force_majeure"]["excused"])
        self.assertEqual(r["analysis"]["checks"]["force_majeure"]["elapsed_hours"], 60.0)

    def test_lower_survey_figure_limits_the_settlement(self):
        a = run("CASE-1006")["analysis"]
        self.assertEqual(a["payout"]["supported_loss"], 210000.0)
        self.assertIn("amount_mismatch", [d["kind"] for d in a["discrepancies"]])
        self.assertFalse(a["checks"]["insurer_notice"]["compliant"])

    def test_unresolvable_conflict_escalates_without_a_payout(self):
        r = run("CASE-1005")
        self.assertEqual(blockers(r), {"unresolved_clause_conflict"})
        self.assertIsNone(r["analysis"]["payout"])
        self.assertFalse(r["analysis"]["decision"]["executable"])
        self.assertIn("ADD-NORTHSTAR-2024-A#1.1", r["memo"])
        self.assertIn("ADD-NORTHSTAR-2024-B#1.1", r["memo"])

    def test_missing_evidence_requests_information(self):
        self.assertEqual(blockers(run("CASE-1007")), {"missing_required_document"})
        self.assertIsNone(run("CASE-1007")["analysis"]["payout"])

    def test_unreadable_vendor_agreement_blocks_the_master_agreement_fallback(self):
        r = run("CASE-1011")
        self.assertEqual(blockers(r), {"unreadable_vendor_document"})
        self.assertIn("VA-ZENITH-2024", r["analysis"]["blockers"][0]["detail"])
        self.assertIsNone(r["analysis"]["payout"])

    def test_planted_instructions_do_not_change_the_outcome_but_add_review(self):
        r = run("CASE-1004")
        self.assertEqual((r["decision"], r["amount"]), ("approve_settlement", 30000.0))
        self.assertIn("legal_counsel", r["proposal"]["content"]["required_approvals"])
        self.assertIn("SECURITY", r["memo"])
        self.assertIn("injection_suspected", r["proposal"]["content"]["flags"])

    def test_every_memo_passes_independent_review(self):
        for cid in GOLDEN:
            r = run(cid)
            self.assertEqual(critic.review_memo(r["memo"], r["analysis"], C.known_ids()), [], cid)
            self.assertIn(SENTENCE, r["memo"])

    def test_every_proposal_cites_only_real_evidence(self):
        known = C.known_ids()
        for cid in GOLDEN:
            c = run(cid)["proposal"]["content"]
            self.assertTrue(set(c["clause_ids"]) <= known["clause"], cid)
            self.assertTrue(set(c["regulation_ids"]) <= known["regulation"], cid)
            self.assertTrue(set(c["ruling_ids"]) <= known["ruling"], cid)


class TestOrchestration(unittest.TestCase):
    def test_runs_are_deterministic(self):
        a, b = Orchestrator(C).run("CASE-1001"), Orchestrator(C).run("CASE-1001")
        self.assertEqual((a["proposal"]["proposal_id"], a["memo"]), (b["proposal"]["proposal_id"], b["memo"]))

    def test_trace_has_every_stage_in_order(self):
        names = [t["agent"] for t in run("CASE-1001")["trace"]]
        self.assertEqual(names, ["intake", "planner", "clause_researcher", "regulation_researcher", "precedent_researcher",
                                 "conflict_resolver", "analyst", "drafter", "critic", "proposal_builder"])
        self.assertTrue(all(t["status"] in ("ok",) for t in run("CASE-1001")["trace"]))

    def test_everything_is_persisted_and_audited(self):
        store = Store(":memory:")
        orch = Orchestrator(C, store)
        r = orch.run("CASE-1001")
        self.assertEqual(store.get_run(r["run_id"])["decision"], "partial_settlement")
        self.assertEqual(store.get_proposal(r["proposal"]["proposal_id"])["status"], "pending_approval")
        events = [x["event"] for x in store.audit_records(run_id=r["run_id"])]
        self.assertEqual(events[0], "run_started")
        self.assertEqual(events.count("agent_completed"), 9)
        self.assertEqual(events[-1], "run_completed")
        self.assertTrue(store.verify_audit()["valid"])

    def test_rerunning_a_case_reuses_the_proposal(self):
        store = Store(":memory:")
        orch = Orchestrator(C, store)
        a, b = orch.run("CASE-1001"), orch.run("CASE-1001")
        self.assertEqual(a["proposal"]["proposal_id"], b["proposal"]["proposal_id"])
        self.assertEqual(len(store.list_proposals()), 1)
        self.assertIn("proposal_reused", [x["event"] for x in store.audit_records()])

    def test_audit_trail_contains_no_document_text_or_contact_details(self):
        store = Store(":memory:")
        orch = Orchestrator(C, store)
        for cid in ("CASE-1001", "CASE-1004", "CASE-1002"):
            orch.run(cid)
        blob = json.dumps(store.audit_records())
        for secret in ("@acme-freight.example", "@glcc.example", "Container ACMU", "water damage", "SYSTEM INSTRUCTION", "ignore all previous"):
            self.assertNotIn(secret, blob, secret)

    def test_researchers_run_concurrently(self):
        started = []

        def slow(self, ctx):                                     # patched in as a method, so it receives the agent too
            started.append(time.perf_counter())
            time.sleep(0.3)
            return {}
        t0 = time.perf_counter()
        with patch.object(ClauseResearcher, "run", slow), patch.object(RegulationResearcher, "run", slow), patch.object(PrecedentResearcher, "run", slow):
            r = Orchestrator(C).run("CASE-1001")
        elapsed = time.perf_counter() - t0
        research = [t for t in r["trace"] if t["agent"].endswith("_researcher")]
        self.assertEqual([t["status"] for t in research], ["ok", "ok", "ok"])        # they really ran (the first version of this test did not)
        self.assertEqual(len(started), 3)
        self.assertLess(max(started) - min(started), 0.15)       # all three started together
        self.assertGreaterEqual(elapsed, 0.3)                    # and the stage took one sleep, not zero
        self.assertLess(elapsed, 0.8)                            # not three in series (0.9s)


class TestWiring(unittest.TestCase):
    def test_every_agent_reads_only_what_an_earlier_stage_wrote(self):
        available: set[str] = set()

        def satisfied(read: str) -> bool:
            return any(w == read or w.startswith(read + ".") or read.startswith(w.split(".")[0] + ".") and w.split(".")[0] == read.split(".")[0] for w in available)

        for agent in PIPELINE_SERIAL_1:
            for r in agent.reads:
                self.assertTrue(satisfied(r), f"{agent.name} reads {r}")
            available |= set(agent.writes)
        for agent in RESEARCHERS:                                  # researchers run in parallel: they may not read each other
            for r in agent.reads:
                self.assertTrue(satisfied(r), f"{agent.name} reads {r}")
        for agent in RESEARCHERS:
            available |= set(agent.writes)
        for agent in PIPELINE_SERIAL_2:
            for r in agent.reads:
                self.assertTrue(satisfied(r), f"{agent.name} reads {r}")
            available |= set(agent.writes)

    def test_researchers_are_independent_and_non_critical(self):
        written = [w for a in RESEARCHERS for w in a.writes]
        self.assertEqual(len(written), len(set(written)))
        self.assertTrue(all(not a.critical for a in RESEARCHERS))
        for a in RESEARCHERS:
            self.assertFalse(any(r.startswith("retrieved") for r in a.reads))

    def test_exactly_one_agent_writes_the_analysis(self):
        writers = [a.name for a in PIPELINE_SERIAL_1 + RESEARCHERS + PIPELINE_SERIAL_2 if "analysis" in a.writes]
        self.assertEqual(writers, ["analyst"])


class TestFailurePolicy(unittest.TestCase):
    def test_unknown_case_fails_cleanly_with_no_proposal(self):
        store = Store(":memory:")
        r = Orchestrator(C, store).run("CASE-9999")
        self.assertEqual((r["status"], r["proposal"]), ("failed", None))
        self.assertIn("unknown case", r["error"])
        self.assertIn("run_failed", [x["event"] for x in store.audit_records()])
        self.assertEqual(store.list_proposals(), [])

    def test_path_traversal_case_ids_are_refused(self):
        for bad in ("../data", "CASE-1001/../CASE-1002", ""):
            self.assertEqual(Orchestrator(C).run(bad)["status"], "failed", bad)

    def test_clause_researcher_failure_becomes_an_escalation_not_a_guess(self):
        with patch.object(ClauseResearcher, "run", side_effect=RuntimeError("search backend down")):
            r = Orchestrator(C).run("CASE-1001")
        self.assertEqual(r["decision"], "escalate_to_legal")
        self.assertIn("retrieval_gap", blockers(r))
        self.assertIsNone(r["analysis"]["payout"])

    def test_researcher_timeout_becomes_a_gap(self):
        def slow(self, ctx):
            time.sleep(0.4)
        with patch.object(ClauseResearcher, "run", slow):
            r = Orchestrator(C, research_timeout_s=0.05).run("CASE-1001")
        self.assertEqual(r["decision"], "escalate_to_legal")
        self.assertTrue(any("timed out" in b["detail"] for b in r["analysis"]["blockers"]))

    def test_precedent_failure_only_loses_precedents(self):
        with patch.object(PrecedentResearcher, "run", side_effect=RuntimeError("boom")):
            r = Orchestrator(C).run("CASE-1001")
        self.assertEqual((r["decision"], r["amount"]), ("partial_settlement", 89000.0))
        self.assertEqual(r["analysis"]["evidence"]["rulings"], [])
        self.assertTrue(any("precedent_researcher failed" in n for n in r["notes"]))

    def test_regulation_failure_on_a_customs_hold_escalates(self):
        with patch.object(RegulationResearcher, "run", side_effect=RuntimeError("boom")):
            r = Orchestrator(C).run("CASE-1002")
        self.assertEqual(r["decision"], "escalate_to_legal")

    def test_a_crash_in_a_critical_agent_ends_in_a_safe_escalation(self):
        for agent_cls in (AnalystAgent, DrafterAgent):
            with patch.object(agent_cls, "run", side_effect=RuntimeError("unexpected bug")):
                store = Store(":memory:")
                r = Orchestrator(C, store).run("CASE-1001")
            self.assertEqual(r["status"], "escalated", agent_cls.__name__)
            c = r["proposal"]["content"]
            self.assertEqual((c["decision_type"], c["amount"], c["blockers"]), ("escalate_to_legal", 0.0, ["system_error"]))
            self.assertIn("run_escalated", [x["event"] for x in store.audit_records()])

    def test_a_memo_that_fails_review_never_becomes_a_proposal(self):
        with patch("glcc.agents.drafting.critic.review_memo", return_value=["invented figure 1,234,567"]):
            r = Orchestrator(C).run("CASE-1001")
        self.assertEqual((r["status"], r["decision"]), ("escalated", "escalate_to_legal"))
        self.assertEqual(r["proposal"]["content"]["blockers"], ["memo_failed_review"])

    def test_a_refused_proposal_escalates(self):
        original = build_proposal
        calls = []

        def refuse_first(*a, **k):
            calls.append(1)
            return (None, ["Unknown clause id(s): ['X#1']"]) if len(calls) == 1 else original(*a, **k)
        with patch("glcc.agents.orchestrator.build_proposal", refuse_first):
            r = Orchestrator(C).run("CASE-1001")
        self.assertEqual((r["status"], r["decision"]), ("escalated", "escalate_to_legal"))
        self.assertEqual(r["proposal"]["content"]["blockers"], ["proposal_rejected"])

    def test_escalation_proposals_are_never_executable(self):
        with patch.object(AnalystAgent, "run", side_effect=RuntimeError("bug")):
            r = Orchestrator(C).run("CASE-1001")
        self.assertFalse(r["analysis"]["decision"]["executable"])


class TestDynamicRag(unittest.TestCase):
    def test_a_missing_required_topic_triggers_reformulation_and_recovers(self):
        real = ClauseRetriever.search

        def flaky(self, query, *, vendor="any", topic=None, k=5, mode="hybrid", reformulate=False):
            if topic == "liability_cap" and not reformulate and query.startswith("limit of liability"):
                return []                                           # the first, plain query finds nothing
            return real(self, query, vendor=vendor, topic=topic, k=k, mode=mode, reformulate=reformulate)
        with patch.object(ClauseRetriever, "search", flaky):
            r = Orchestrator(C).run("CASE-1001")
        prov = [p for p in r["retrieval"]["clause_provenance"] if p["topic"] == "liability_cap"]
        self.assertEqual([p["iteration"] for p in prov], [1, 2])
        self.assertTrue(prov[1]["reformulated"])
        self.assertEqual((r["decision"], r["amount"]), ("partial_settlement", 89000.0))

    def test_a_topic_that_cannot_be_found_is_a_reported_gap_not_a_guess(self):
        trimmed = Corpus(root=C.root, vendors=C.vendors, clauses=[c for c in C.clauses if c.topic != "insurance_coverage"],
                         reg_sections=C.reg_sections, rulings=C.rulings, unreadable_documents=C.unreadable_documents)
        r = Orchestrator(trimmed).run("CASE-1001")
        self.assertEqual(r["decision"], "escalate_to_legal")
        self.assertIn("retrieval_gap", blockers(r))
        iters = {p["iteration"] for p in r["retrieval"]["clause_provenance"] if p["topic"] == "insurance_coverage"}
        self.assertEqual(iters, {1, 2, 3})                          # tried the plain query, the expansion, then the bare topic

    def test_supersession_expansion_pulls_in_clauses_that_search_missed(self):
        base = C.clause("VA-ACME-2022#5.1")
        expanded, added = expand_by_supersession(C, [base])
        self.assertIn("VA-ACME-2022-AMD2#1.1", {c.clause_id for c in expanded})
        self.assertEqual(added, ["VA-ACME-2022-AMD2#1.1"])
        ns, _ = expand_by_supersession(C, [C.clause("VA-NORTHSTAR-2023#4.1")])
        self.assertTrue({"ADD-NORTHSTAR-2024-A#1.1", "ADD-NORTHSTAR-2024-B#1.1"} <= {c.clause_id for c in ns})
        lone, none_added = expand_by_supersession(C, [C.clause("MSA-GLCC-001#5.1")])
        self.assertEqual((len(lone), none_added), (1, []))

    def test_an_amendment_the_search_never_returned_is_still_applied(self):
        real = ClauseRetriever.search

        def hide_amendment(self, *a, **k):
            return [h for h in real(self, *a, **k) if h.item.clause_id != "VA-ACME-2022-AMD2#1.1"]
        with patch.object(ClauseRetriever, "search", hide_amendment):
            r = Orchestrator(C).run("CASE-1001")
        self.assertEqual((r["decision"], r["amount"]), ("partial_settlement", 89000.0))
        self.assertEqual(r["analysis"]["governing"]["liability_cap"]["governing_clause"], "VA-ACME-2022-AMD2#1.1")


class TestLlmModes(unittest.TestCase):
    def llm(self, mode, **kw):
        return no_sleep_llm(SimulatedLLM(mode), max_retries=1, **kw)

    def test_the_model_can_never_change_the_outcome(self):
        base = run("CASE-1001")
        for mode in MODES:
            for cid in ("CASE-1001", "CASE-1004", "CASE-1005"):
                r = Orchestrator(C, llm=self.llm(mode)).run(cid)
                off = run(cid)
                self.assertEqual((r["status"], r["decision"], r["amount"]), (off["status"], off["decision"], off["amount"]), (mode, cid))
                self.assertEqual(r["proposal"]["content"]["required_approvals"], off["proposal"]["content"]["required_approvals"], (mode, cid))
        self.assertEqual(base["amount"], 89000.0)

    def test_a_good_model_adds_a_grounded_fact_and_a_narrative(self):
        r = Orchestrator(C, llm=self.llm("good")).run("CASE-1001")
        intake = next(t for t in r["trace"] if t["agent"] == "intake")["summary"]
        self.assertEqual((intake["llm"]["accepted"], intake["llm"]["rejected"]), (1, []))
        self.assertEqual(r["memo_meta"]["narrative"], "llm")
        self.assertIn("The evidence supports the decision partial_settlement", r["memo"])
        self.assertEqual(r["mode"], "llm")

    def test_hallucinated_facts_and_narratives_are_rejected(self):
        r = Orchestrator(C, store := Store(":memory:"), llm=self.llm("hallucinating")).run("CASE-1001")
        intake = next(t for t in r["trace"] if t["agent"] == "intake")["summary"]["llm"]
        reasons = " | ".join(x["reason"] for x in intake["rejected"])
        self.assertEqual(intake["accepted"], 0)
        for expect in ("not found verbatim", "invalid timestamp", "not allowed", "unknown document"):
            self.assertIn(expect, reasons)
        self.assertEqual(r["memo_meta"]["narrative"], "rejected")
        self.assertNotIn("777,777", r["memo"])
        self.assertNotIn("CLAUSE-FAKE-999", r["memo"])
        events = [x["event"] for x in store.audit_records()]
        self.assertEqual(events.count("llm_output_rejected"), 2)

    def test_a_model_claiming_the_payout_was_released_is_rejected(self):
        r = Orchestrator(C, llm=self.llm("malicious")).run("CASE-1001")
        self.assertEqual(r["memo_meta"]["narrative"], "rejected")
        self.assertIn("already carried out", r["memo_meta"]["reasons"][0])
        self.assertNotIn("has been released", r["memo"])

    def test_provider_outage_and_malformed_output_fall_back_to_the_deterministic_path(self):
        for mode in ("down", "malformed"):
            store = Store(":memory:")
            r = Orchestrator(C, store, llm=self.llm(mode)).run("CASE-1001")
            self.assertEqual(r["status"], "completed", mode)
            self.assertEqual(r["memo_meta"]["narrative"], "unavailable")
            self.assertIn("llm_fallback", [x["event"] for x in store.audit_records()])
            self.assertEqual(r["llm_stats"]["retries"], 2)

    def test_circuit_breaker_stops_hammering_a_failing_provider(self):
        clock = [0.0]
        llm = ResilientLLM(SimulatedLLM("down"), max_retries=0, failure_threshold=2, cooldown_s=30, sleep=lambda s: None, clock=lambda: clock[0])
        orch = Orchestrator(C, llm=llm)
        orch.run("CASE-1001")                                       # two failed calls (intake, drafter) open the breaker
        self.assertTrue(llm.breaker_open)
        before = llm.stats["calls"]
        r = orch.run("CASE-1002")
        self.assertEqual((r["status"], r["decision"]), ("completed", "clear_customs_hold"))
        self.assertEqual(llm.stats["calls"], before)                # nothing was sent to the failing provider
        self.assertGreaterEqual(llm.stats["short_circuited"], 2)
        clock[0] = 31.0
        self.assertFalse(llm.breaker_open)                          # half-open again after the cooldown

    def test_prompts_carry_redacted_untrusted_documents(self):
        spy = ScriptedLLM([LLMError("stop"), LLMError("stop")])
        Orchestrator(C, llm=no_sleep_llm(spy, max_retries=0)).run("CASE-1001")
        system, user = spy.calls[0]
        self.assertIn("UNTRUSTED", system)
        self.assertIn("<untrusted_documents>", user)
        self.assertNotIn("ops@acme-freight.example", user)
        self.assertIn("[REDACTED-EMAIL]", user)

    def test_injected_instructions_reach_the_model_only_as_data_and_change_nothing(self):
        spy = ScriptedLLM([{"events": []}, {"summary": "Settlement supported.", "explanation": "See the reasoning chain."}])
        r = Orchestrator(C, llm=no_sleep_llm(spy)).run("CASE-1004")
        self.assertIn("ignore all previous instructions", spy.calls[0][1])        # it is in the data...
        self.assertEqual((r["decision"], r["amount"]), ("approve_settlement", 30000.0))   # ...and nowhere in the logic
        self.assertIn("legal_counsel", r["proposal"]["content"]["required_approvals"])

    def test_a_narrative_may_cite_real_evidence_but_not_invent_figures(self):
        ok = ScriptedLLM([{"events": []}, {"summary": "MSA-GLCC-001#3.1 was overridden by the amendment.", "explanation": "Recovery is 89,000.00."}])
        self.assertEqual(Orchestrator(C, llm=no_sleep_llm(ok)).run("CASE-1001")["memo_meta"]["narrative"], "llm")
        bad = ScriptedLLM([{"events": []}, {"summary": "Pay 123,456 now.", "explanation": "x"}])
        r = Orchestrator(C, llm=no_sleep_llm(bad)).run("CASE-1001")
        self.assertEqual(r["memo_meta"]["narrative"], "rejected")
        self.assertIn("123456", r["memo_meta"]["reasons"][0])

    def test_a_narrative_citing_a_real_id_from_another_case_is_rejected(self):
        spy = ScriptedLLM([{"events": []}, {"summary": "See VA-NORTHSTAR-2023#4.1.", "explanation": "x"}])
        r = Orchestrator(C, llm=no_sleep_llm(spy)).run("CASE-1001")
        self.assertEqual(r["memo_meta"]["narrative"], "rejected")
        self.assertIn("not part of the evidence", r["memo_meta"]["reasons"][0])

    def test_critic_drops_a_narrative_that_makes_the_memo_fail_review(self):
        analysis = run("CASE-1001")["analysis"]
        ctx = RunContext(corpus=C, case_id="CASE-1001")
        ctx.analysis = analysis
        ctx.memo = render_memo(analysis).replace(SENTENCE, "")     # a memo missing the mandatory statement
        ctx.memo_meta = {"narrative": "llm"}
        out = CriticAgent().run(ctx)
        self.assertTrue(out["passed"])
        self.assertTrue(out["redrafted_without_narrative"])
        self.assertIn(SENTENCE, ctx.memo)
        self.assertEqual(ctx.memo_meta["narrative"], "dropped_by_critic")


class TestCritic(unittest.TestCase):
    def setUp(self):
        self.r = run("CASE-1001")
        self.a, self.memo, self.known = self.r["analysis"], self.r["memo"], C.known_ids()

    def review(self, memo):
        return critic.review_memo(memo, self.a, self.known)

    def test_clean_memo_passes(self):
        self.assertEqual(self.review(self.memo), [])

    def test_historical_rulings_may_say_a_claim_was_paid(self):
        self.assertIn("was paid", self.memo)                       # from a precedent summary
        self.assertEqual(self.review(self.memo), [])

    def test_invented_figure_fails(self):
        self.assertTrue(any("figure" in i for i in self.review(self.memo + "\nRecovery is 250,000.00.")))

    def test_unknown_and_foreign_ids_fail(self):
        self.assertTrue(any("does not exist" in i for i in self.review(self.memo + " See CLAUSE-NOPE-1#1.1.")))
        self.assertTrue(any("not part of the evidence" in i for i in self.review(self.memo + " See VA-NORTHSTAR-2023#4.1.")))

    def test_executed_claims_in_the_narrative_fail(self):
        bad = self.memo.replace("## Summary\n\n", "## Summary\n\nThe payout has been released. ")
        self.assertTrue(any("already carried out" in i for i in self.review(bad)))

    def test_missing_statement_decision_amount_or_reviewer_fail(self):
        self.assertTrue(any("mandatory" in i for i in self.review(self.memo.replace(SENTENCE, ""))))
        self.assertTrue(any("decision type" in i for i in self.review(self.memo.replace("`partial_settlement`", "x"))))
        self.assertTrue(any("amount" in i for i in self.review(self.memo.replace("89,000.00", "89,000"))))
        self.assertTrue(any("underwriter" in i for i in self.review(self.memo.replace("underwriter", "someone"))))

    def test_omitted_blockers_discrepancies_and_security_findings_fail(self):
        a1005 = run("CASE-1005")
        self.assertTrue(any("unresolved_clause_conflict" in i for i in
                            critic.review_memo(a1005["memo"].replace("unresolved_clause_conflict", "x"), a1005["analysis"], self.known)))
        a1006 = run("CASE-1006")
        self.assertTrue(any("amount_mismatch" in i for i in
                            critic.review_memo(a1006["memo"].replace("amount_mismatch", "x"), a1006["analysis"], self.known)))
        a1004 = run("CASE-1004")
        self.assertTrue(any("security" in i for i in
                            critic.review_memo(a1004["memo"].replace("SECURITY", "x"), a1004["analysis"], self.known)))

    def test_number_check_ignores_ids_dates_and_container_numbers(self):
        self.assertEqual(critic.numbers_in("VA-ACME-2022#5.1 shipped 2024-05-10 container ACMU-4471 RULING-2023-014"), set())
        self.assertEqual(critic.numbers_in("pay 89,000.00 and 1200"), {"89000", "1200"})


class TestProposalBuilder(unittest.TestCase):
    def setUp(self):
        self.a = json.loads(json.dumps(run("CASE-1001")["analysis"]))
        self.memo = run("CASE-1001")["memo"]

    def build(self, a=None, memo=None, as_of=None):
        return build_proposal(C, a or self.a, memo if memo is not None else self.memo, as_of)

    def test_valid_and_deterministic(self):
        p1, e1 = self.build()
        p2, _ = self.build()
        self.assertEqual((e1, p1["proposal_id"]), ([], p2["proposal_id"]))
        self.assertTrue(p1["proposal_id"].startswith("PROP-"))

    def test_changing_the_memo_changes_the_proposal(self):
        self.assertNotEqual(self.build()[0]["proposal_id"], self.build(memo=self.memo + "\nextra")[0]["proposal_id"])

    def test_invented_citations_are_refused(self):
        self.a["evidence"]["relied_clauses"] = ["CLAUSE-MADE-UP#1.1"]
        self.a["evidence"]["rulings"] = ["RULING-9999-999"]
        p, errors = self.build()
        self.assertIsNone(p)
        self.assertEqual(sum("Unknown" in e for e in errors), 2)

    def test_a_regulation_not_in_force_is_refused(self):
        a = json.loads(json.dumps(run("CASE-1002")["analysis"]))
        p, errors = build_proposal(C, a, run("CASE-1002")["memo"], "2024-11-04")        # v2 did not exist yet
        self.assertIsNone(p)
        self.assertTrue(any("not in force" in e for e in errors))

    def test_amount_must_match_the_calculation_and_not_exceed_the_claim(self):
        self.a["decision"]["amount"] = 95000.0
        self.assertTrue(any("does not match the calculated" in e for e in self.build()[1]))
        self.a["decision"]["amount"] = 999999.0
        self.assertTrue(any("exceeds the claimed" in e for e in self.build()[1]))

    def test_an_executable_decision_needs_a_governing_citation(self):
        self.a["evidence"]["relied_clauses"], self.a["evidence"]["regulations"] = [], []
        self.assertTrue(any("must cite" in e for e in self.build()[1]))

    def test_unknown_decision_unknown_case_and_empty_memo(self):
        self.a["decision"]["decision_type"] = "wire_money"
        self.assertTrue(any("Unknown decision" in e for e in self.build()[1]))
        a2 = json.loads(json.dumps(run("CASE-1001")["analysis"]))
        a2["case_id"] = "CASE-9999"
        self.assertTrue(any("Unknown case" in e for e in self.build(a2)[1]))
        self.assertTrue(any("memo is empty" in e for e in self.build(memo="  ")[1]))

    def test_escalation_analysis_always_builds(self):
        a = escalation_analysis("CASE-1001", "something broke")
        p, errors = build_proposal(C, a, render_memo(a))
        self.assertEqual(errors, [])
        self.assertEqual(p["content"]["required_approvals"], ["legal_counsel"])
        self.assertEqual(p["content"]["amount"], 0.0)


class TestIntakeGrounding(unittest.TestCase):
    def test_a_grounded_event_from_the_model_is_accepted_and_marked(self):
        email = next(d for d in C.case("CASE-1001").documents if d.doc_id == "email_02_glcc_reply")
        llm = ScriptedLLM([{"events": [{"ts": email.meta["date"], "kind": "stated_amount", "actor": "glcc", "doc_id": email.doc_id,
                                        "quote": "We estimate the cargo loss at USD 95,000"}]}])
        ctx = RunContext(corpus=C, case_id="CASE-1001", llm=no_sleep_llm(llm))
        IntakeAgent().run(ctx)
        added = [e for e in ctx.facts.events if e.attr("source") == "llm"]
        self.assertEqual([(e.kind, e.doc_id) for e in added], [("stated_amount", "email_02_glcc_reply")])

    def test_duplicates_of_baseline_events_are_not_added_twice(self):
        llm = ScriptedLLM([{"events": [{"ts": "2024-05-22 15:00", "kind": "insurer_notice", "actor": "glcc", "doc_id": "chat_claims",
                                        "quote": "notified the cargo insurer"}]}])
        ctx = RunContext(corpus=C, case_id="CASE-1001", llm=no_sleep_llm(llm))
        out = IntakeAgent().run(ctx)
        self.assertEqual(out["llm"]["accepted"], 0)
        self.assertIn("duplicate", out["llm"]["rejected"][0]["reason"])

    def test_events_that_quote_text_only_present_in_another_document_are_rejected(self):
        llm = ScriptedLLM([{"events": [{"ts": "2024-05-22 15:00", "kind": "stated_amount", "actor": "glcc", "doc_id": "survey_independent",
                                        "quote": "notified the cargo insurer"}]}])
        ctx = RunContext(corpus=C, case_id="CASE-1001", llm=no_sleep_llm(llm))
        self.assertIn("not found verbatim", IntakeAgent().run(ctx)["llm"]["rejected"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
