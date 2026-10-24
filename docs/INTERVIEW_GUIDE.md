# Explaining this project

The assignment says you may use AI tools but must be able to explain and justify the code and the decisions. This guide is for that.
**It is not a substitute for reading and running the code.** If an interviewer asks you to change something live, only your own
understanding helps. Budget two to three hours to do the study plan below.

## Say clearly how it was built

A short, honest version: "I built this with an AI coding assistant. I reviewed the code, ran it, and kept the tests the build produced,
including mutation checks that deliberately break the code. Here is what I verified and what I did not." **Only say what is true for you.**
If you did not specify the architecture yourself, do not claim you did; say you reviewed it and can explain each decision. Then be exact about the limits (no Azure deployment, no real LLM run, lexical retrieval only). Overclaiming
is the fastest way to lose an interview; the limits section of the README is written to make this easy.

## One-minute pitch

"GLCC resolves disruptions by reading contracts, emails, and regulations. I built a pipeline of agents that retrieves the relevant
clauses, regulation version, and past rulings, works out which clause *actually governs* when contracts conflict, computes the payout in
code, and produces a cited proposal. It never executes anything: humans approve under an authority matrix, execution is gated and
once-only, and everything is in a tamper-evident audit log. A language model is optional and treated as untrusted, so the answer never
depends on it. The same property is what lets it run and be tested without credentials."

## Study plan (about 2.5 hours)

1. **Run it (15 min).** `python -m unittest discover -s tests`, `python -m glcc demo`, then read `reports/DEMO_REPORT.md` top to bottom.
2. **Read in this order (75 min):**
   1. `glcc/reasoning/precedence.py` (80 lines: the heart of "conflicting clauses")
   2. `glcc/reasoning/liability.py` then `analysis.py` (how facts and clauses become a decision, and where blockers come from)
   3. `glcc/ingest/contracts.py` (how a PDF becomes clauses, topics, terms, supersession)
   4. `glcc/retrieval/index.py` and `retrievers.py` (BM25, trigram, RRF, filters)
   5. `glcc/agents/orchestrator.py` and `research.py` (coordination, failure policy, the repair loop)
   6. `glcc/agents/critic.py` and `intake.py` (how model output is verified)
   7. `glcc/governance/authority.py` and `store.py` (the execution gate and the audit chain)
3. **Break it yourself (30 min).** Run `python scripts/mutation_check.py`. Then make two changes by hand and watch which test fails: for
   example, change `COORDINATOR_LIMIT` in `authority.py`, and change `rule 3` in `precedence.py`. Being able to predict the failure is the point.
4. **Add something small (30 min).** For example a new clause in `scripts/make_sample_data.py` plus a case, or a new decision rule.
   If you can add a scenario and its test, you understand the system.

## Questions you should expect

**Why not just give the contracts to an LLM and ask?**
Because the answer moves money and must be reproducible and explainable. A model cannot be regression-tested on "which of three similar
caps applies" the way code can, it can be wrong with full confidence, and documents can contain instructions aimed at it. So the model
never decides. It may add facts that quote their source, and write narrative that a critic checks.

**What makes it "multi-agent"? Isn't it just a pipeline?**
It is a pipeline of specialized agents with a shared blackboard, which suits a fixed workflow. What makes it agentic is that stages make
decisions about what to do next: the planner chooses queries from the facts, the researchers run in parallel and *repair* their own
retrieval when a required topic comes back empty, the analyst turns "I cannot tell" into the correct handoff, the critic can reject the
drafter's output, and the orchestrator applies a failure policy. I chose a fixed pipeline over agents chatting because every handoff
is then named, traced, and testable. A LangGraph or Agent Framework graph would express the same stages.

**How do you handle conflicting clauses?**
Five ordered rules, each recording why a clause lost: other vendor, not yet applicable to this shipment, explicitly replaced, vendor-specific
over general (the master agreement states this itself in clause 1.2), later effective date. If more than one clause survives, it is unresolved
and goes to legal. Time matters: an amendment effective 1 March does not apply to cargo shipped in February, so the original clause governs.

**How do you know retrieval is good?**
26 labeled queries with typos, hard negatives, paraphrases, temporal and port-filter cases; Recall/MRR/nDCG for BM25, trigram, and hybrid.
All are near ceiling on a tiny corpus, and the result does **not** show hybrid beating trigram alone; it shows BM25 alone fails typos.
I would not claim more. It is a regression gate. For production I would add embeddings and build the labeled set from real analyst searches.

**What about hallucination?**
Three layers. Facts a model adds must quote the source verbatim, or they are dropped. A narrative must cite only ids that belong to this case
and may contain only figures that appear in the analysis, and must not claim anything was paid or released. And a proposal cannot be created
if it cites anything that does not exist, so even a bug cannot produce a fabricated citation. A simulated hallucinating model never changes
the decision (`reports/DEMO_REPORT.md`, section 4).

**What about prompt injection?**
Documents are untrusted data: scanned for instruction-like text, redacted before any model sees them, wrapped as data, and the model has no
tools. A planted "approve in full" (CASE-1004) is flagged, changes nothing, and adds legal review. The real backstop is structural: no model
output can trigger an action, because execution needs human approvals.

**What stops the system paying twice, or paying the wrong thing?**
Approvals bind to the proposal's content hash; editing it voids them. Execution has a primary key per proposal and runs in one transaction with
its audit record, so a retry or ten simultaneous requests produce one payout (tested). The amount is re-checked against the calculation when the
proposal is built.

**Why SQLite and the standard-library server?**
So it runs with one dependency and I could test the API over real sockets. They are replaceable: the service layer is framework-independent.
SQLite is single-writer, which is why the Azure deployment is pinned to one replica. Production would use Postgres.

**Is it deployed on Azure?**
No. I wrote the Container Apps deployment, Dockerfile, and workflows, but did not have a subscription to test them, and I say so. What I verified is
the local system and the server process. The Azure-specific code is small and isolated.

**What would you change first?**
Embeddings; OCR for scans; legal and finance sign-off on the precedence rules and the settlement formula; Postgres and an immutable audit anchor;
Entra group-based roles; a feedback loop from human overrides.

**What is the weakest part?**
Offline extraction is pattern-based and written against the sample document formats, so real-world variety is the main risk; the LLM path exists for
that, but it has only been exercised with fakes. And the sample rules and formula are mine, not the business's.

## Things the code deliberately does that are worth pointing out

* `request_information` and `escalate_to_legal` are first-class outcomes, not errors.
* An unreadable vendor agreement blocks the master-agreement fallback (CASE-1011): the unread document might override it.
* Regulation versions are chosen by the **incident date**, and a superseded one cannot even be cited.
* Refusals and blocked attempts are audited, not just successes.
* The critic's "executed" scan applies to the narrative, because historical rulings legitimately say a claim "was paid".
* The test suite was mutation-checked, because tests that cannot fail prove nothing. One of the build's own tests (concurrent research) turned out to be vacuous: its stub raised
  immediately, so it passed without ever running in parallel. It was found when a sibling test failed, fixed, and the mutation check now guards that behavior.
