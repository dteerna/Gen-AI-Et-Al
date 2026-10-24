# How each requirement is demonstrated

Run `python -m glcc demo --out reports` to regenerate every artifact referenced here from a real run.

## 1. The five behaviors the assignment asks to see

| Behavior | Demonstration | Evidence | Tests |
|---|---|---|---|
| **Conflicting clauses** | CASE-1001: three liability caps (50,000 master, 120,000 vendor, 80,000 amendment); the amendment governs and the other two are overridden with stated reasons. CASE-1009: same vendor, cargo shipped earlier; the *original* cap governs. CASE-1005: two equal addenda; the system refuses to choose | `reports/memos/CASE-1001.md` section "Clauses that do not govern" | `test_reasoning.py::TestPrecedence` (13), `test_agents.py::TestGoldenOutcomes` |
| **Retrieval accuracy** | 26 labeled queries over typos, hard negatives (distractor clauses sharing words), paraphrases, temporal regulation lookups, port filters; BM25 vs trigram vs hybrid, Recall/MRR/nDCG at k=3 and k=5 | `reports/retrieval_eval.md`, `python -m glcc retrieval-eval` | `test_retrieval.py::TestQualityGate` (fails the build if quality drops) |
| **Exception scenarios** | 11 cases covering damage, customs, delay, high value, missing evidence, unreadable agreement, planted instructions | `reports/DEMO_REPORT.md` section 1 | `TestGoldenOutcomes` asserts decision, amount, reviewers, and flags for every case |
| **Audit trails** | Hash-chained log; per-run trace; approvals bound to content hash; refusals, blocks, replays, and failures all recorded; an exported log re-verifies independently | `reports/audit_export.jsonl`, `python -m glcc audit --verify` | `test_store.py::TestAuditChain`, `test_cli_demo.py` |
| **Relevant edge cases** | Catalogued below | `reports/DEMO_REPORT.md` section 3 | see the catalog |

## 2. Edge-case catalog

| Edge case | Behavior | Where tested |
|---|---|---|
| Cargo shipped one day before an amendment applies | Original clause governs; amendment shown as "not yet applicable" | `TestPrecedence.test_boundary_the_day_the_amendment_applies`, CASE-1009 |
| Amendment never returned by search | Pulled in through the "replaces" link, so it still applies | `TestDynamicRag.test_an_amendment_the_search_never_returned_is_still_applied` |
| Amendment applies, but base clause is retrieved alone | Base is eliminated by explicit supersession | `TestPrecedence` |
| Vendor with no agreement of its own | Master agreement applies, per-container cap multiplied by container count | CASE-1008 |
| Vendor agreement exists but only as a scan | Not assumed absent: `request_information` | CASE-1011, `test_ingest.py::TestUnreadableAndBrokenDocuments` |
| Two equal clauses conflict | `escalate_to_legal`, no amount computed | CASE-1005 |
| Regulation superseded | Never returned by retrieval, never citable in a proposal | `TestTemporalRegulations`, `TestProposalBuilder.test_a_regulation_not_in_force_is_refused` |
| Same facts, different regulation version | Judged under the version in force on the hold date | CASE-1002 vs CASE-1010, `TestCustomsCheck` |
| Carrier asserts "within a day"; timestamps say 60h | Discrepancy flagged, timestamps win, legal review added | CASE-1003 |
| Verbal notice only | Never satisfies a written-notice requirement | `TestForceMajeure.test_verbal_notice_alone_never_excuses` |
| Survey below the claimed amount | Settlement basis is the lower figure; flagged | CASE-1006 |
| Claim form disagrees with the system of record | Flagged, confirm with claimant | `TestFacts.test_claim_form_mismatch` |
| Insurer notified late | Reduced coverage percentage | CASE-1006 |
| Loss below the cap | Carrier pays all; insurance not needed | `TestSettlement` |
| Uncovered amount below the deductible | Insurance pays nothing | `TestSettlement` |
| Required evidence missing (no survey) | `request_information` | CASE-1007 |
| Insurer notification date cannot be established | Blocker, no guess | `analysis.py` |
| Unknown vendor | Blocker (vendor-specific terms cannot be ruled out) | `analysis.py` |
| Vendor name spelled many ways | Normalized by the registry | `TestVendorRegistry` |
| Instructions hidden in a document | Flagged, treated as data, extra reviewer, outcome unchanged | CASE-1004, `TestLlmModes` |
| LLM hallucinates facts or ids | Rejected with reasons; outcome unchanged | `TestLlmModes.test_hallucinated_facts_and_narratives_are_rejected` |
| LLM claims the payout was released | Rejected | `test_a_model_claiming_the_payout_was_released_is_rejected` |
| LLM down or malformed | Retries, then deterministic path; circuit breaker | `test_provider_outage_and_malformed_output...`, `test_circuit_breaker...` |
| PII in documents sent to a model | Redacted; documents wrapped as untrusted | `test_prompts_carry_redacted_untrusted_documents` |
| A researcher crashes or times out | Gap, then escalation (or only loses precedents) | `TestFailurePolicy` |
| A critical agent crashes | Safe escalation proposal, non-executable | `test_a_crash_in_a_critical_agent_ends_in_a_safe_escalation` |
| Memo fails independent review | Never becomes a proposal | `test_a_memo_that_fails_review_never_becomes_a_proposal` |
| Proposal cites something that does not exist | Refused | `TestProposalBuilder` |
| Unknown case, path traversal in the case id | Clean failure, no proposal | `TestFailurePolicy`, `TestAnalysisEndpoints` |
| Corrupt PDF, bad `case.json`, malformed regulation | Skipped or reported with a clear error; the rest still loads | `test_ingest.py` |
| Execute before approval | Blocked and audited | `TestExecution` |
| Approver lacks the role / is the system / is the creator | Refused and audited | `TestApprovals`, `TestAuthorityMatrix` |
| One person tries to fill two dual-control roles | Blocked | `TestExecutionGate.test_dual_control_needs_two_people` |
| Approval revoked, expired, or for edited content | Blocked | `TestExecution`, `TestExecutionGate` |
| Role removed after approval | Blocked at execution (live re-check) | `test_role_removed_after_approval_blocks_execution` |
| Double click / retry / 10 simultaneous executes | Exactly one execution; the rest return the stored result | `test_concurrent_execution_pays_exactly_once`, `test_ten_simultaneous_execute_requests_pay_once` |
| Payment action fails | Nothing recorded as executed; failure audited; safe to retry | `test_failed_action_records_nothing_as_executed...` |
| Stored proposal altered in the database | Execution blocked (hash mismatch) | `test_tampering_with_stored_content_blocks_execution` |
| Audit record edited, deleted, or reordered | Detected | `TestAuditChain` |
| Oversized or invalid request body | 413 / 400 | `TestRobustness` |
| Internal error in the API | Generic 500; detail only in the server log | `test_internal_errors_do_not_leak_details` |

## 3. Tests that guard the tests

* **Mutation check** (`python scripts/mutation_check.py`): 24 deliberate breakages (for example: stop checking the critic's figure scan, let
  a failing memo become a proposal, run researchers serially, drop API-key enforcement, stop auditing refusals). Each must make the suite
  fail; the script verifies it. All are caught.
* The retrieval **quality gate** fails the build if hybrid Recall@5 falls below 0.95, MRR below 0.90, or temporal queries are not exact.
* Bugs the tests caught while building (kept as regression tests): notification clauses misclassified as detention; a stopword-only
  query matching arbitrary text; unhashable clause objects; refusal audit records rolled back with the failed request; a critic that
  flagged its own mandatory disclaimer and then flagged historical rulings; a concurrency test that passed vacuously because its stub
  raised immediately.

## 4. What this evaluation cannot tell you

The corpus is small and written by the author, the labeled queries are the author's, and there is no real LLM in the loop. The retrieval
numbers are a regression gate and a ranker comparison, not an estimate of production accuracy. See `docs/DESIGN_DECISIONS.md` for the list of limits.
