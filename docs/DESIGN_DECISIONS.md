# Design decisions

Each entry: what was chosen, why, what was rejected, and what it costs.

## 1. "LLM proposes, code disposes"

**Chosen.** Decisions that move money or release cargo (which clause governs, the settlement amount, who must approve) are computed by
deterministic, unit-tested code. A language model is optional and may only (a) add extracted facts that quote their source verbatim and
(b) write a short narrative that a critic verifies.

**Why.** These outcomes must be reproducible, explainable line by line, and testable against known contract structures. A model's
answer to "which cap applies, and what is the payout?" cannot be audited or regression-tested the same way, and it can be wrong with
perfect confidence. Making the model optional also means the whole system runs, and can be graded, without credentials.

**Rejected.** An LLM agent with tools that reads contracts and decides. It demos well and fails exactly where the stakes are:
arithmetic, precedence between near-identical clauses, and prompt injection through documents.

**Cost.** Offline extraction is pattern-based and tuned to the sample formats. Generalizing needs the LLM path and more rules.

## 2. A blackboard pipeline, not free-form agent chat

**Chosen.** Agents read and write named fields on a shared run context. They never call each other. The orchestrator owns order,
parallelism, timeouts, and failure policy. Each agent declares `reads` and `writes`, and a test proves the wiring.

**Why.** Agent-to-agent conversation is hard to test, hard to audit, and nondeterministic. A blackboard gives every handoff a name,
a trace entry, and an audit record, and lets the three retrieval agents genuinely run in parallel (tested, with a timing assertion).

**Rejected.** A supervisor LLM that routes between peer agents (LangGraph or AutoGen style). More flexible, but the routing itself
becomes an unauditable decision. This domain has a fixed workflow, so a fixed pipeline fits.

**Mapping to frameworks.** The same stages map directly to a LangGraph graph or Microsoft Agent Framework workflow if the team
standardizes on one. The custom runner was chosen so every line can be read, explained, and tested here.

## 3. Conflicting clauses are resolved by rules, with an explicit "I cannot decide"

**Chosen.** Eliminate in a fixed order: other vendor, not yet applicable (`applies_from` after the shipment date), explicitly replaced
("replaces Section X of ..."), vendor-specific over general, later effective date. If more than one clause remains, the answer is
**unresolved, escalate**. Every elimination records its reason.

**Why.** The master agreement itself states the vendor-specific-over-general order (clause 1.2), so the rule is grounded in the
contract, not invented. Supersession only counts when the *replacing* clause actually applies, so an amendment that took effect after
the cargo shipped cannot erase the base clause (CASE-1009). The tie case matters most: two addenda with the same date and different
caps (CASE-1005) get no payout and a handoff to legal.

**Cost.** Real contracts have richer hierarchies (jurisdiction, per-lane terms, signature dates). The rules are a tested starting point.

## 4. Time is part of correctness

**Chosen.** Contract clauses carry `applies_from`; regulations carry validity windows; retrieval returns only the version in force on
the incident date and reports what it excluded. Proposals re-check that cited regulations were in force.

**Why.** The commonest legal-RAG failure is citing a superseded rule. CASE-1010 (2024) must use the 10-day rule even though a stricter
5-day rule exists today; CASE-1002 (2025) must use the new one. A unit test shows the same facts judged under both versions give
different answers, so the version genuinely matters.

## 5. Retrieval: lexical hybrid, evaluated honestly

**Chosen.** BM25 over stemmed tokens + character-trigram TF-IDF, fused with Reciprocal Rank Fusion; metadata filters applied *before*
ranking; clause-level chunks (never fixed windows that could split a limit from its condition); topic-filtered queries with a repair loop;
expansion along supersession links.

**Evaluation result.** On 26 labeled queries over a 31-clause corpus, all rankers are near ceiling. BM25 alone loses typo queries
(recall 0.83); trigram alone is as good as the fusion, and has a slightly higher MRR, **so the data does not show fusion beating
trigram**. The report says so. I kept fusion on the expectation that trigram similarity gets noisier on larger corpora while BM25
anchors exact terms, which is an expectation and not a measurement. A first draft of the report claimed more than the data supported;
I corrected it.

**Rejected.** Embeddings, because they need a model or an Azure resource, and the point here was a runnable system with measurable
behavior. They are the first thing to add for paraphrase recall. The `Retriever` interface is narrow enough to swap in Azure AI Search
(keyword + vector + semantic ranking) without touching the agents.

**Cost.** A query sharing no words with the right clause can miss.

## 6. Facts are extracted with provenance and conflict rules

**Chosen.** Every event carries the document id and a verbatim quote (a test asserts each quote exists in its source). Conflicts follow
an evidence hierarchy: official port notice > system timestamp (email header, chat log) > narrative assertion. A carrier's "within a
day" is checked against timestamps (CASE-1003). A survey below the claim limits the settlement basis (CASE-1006).

**Assumptions stated in code:** UTC everywhere; GLCC "discovers" a loss when the carrier first notifies it; business days are Monday to
Friday with no holiday calendar; the party invoking force majeure bears the burden of showing written notice.

## 7. "Unknown" is never a favorable default

A missing document, an unreadable vendor agreement, an unknown vendor, a missing required clause topic, an unestablished fault, or an
unresolved conflict each become a **blocker**, and a blocker produces `request_information` or `escalate_to_legal`. The unreadable
Zenith agreement (CASE-1011) is the sharp example: the master agreement is *not* assumed to apply, because the unread document might
override it.

## 8. Approvals are cryptographically bound, expiring, and re-checked

Approvals bind to the SHA-256 of the exact proposal content, so editing the proposal voids them. They expire after 72 hours, can be
revoked only by their author, and are re-validated at execution time against the live role directory (a role removed after approval
blocks execution). Dual control requires *different people* for different roles. The system identity and the proposal's creator can
never approve. Execution is idempotent via a primary key, and the action runs in the same transaction as its audit record.

**Why not a boolean "approved" flag.** It cannot say who, for what content, or until when, and it survives edits.

## 9. Audit: a hash chain, plus honest limits

Each record stores the previous record's hash and a sequence number. Editing, deleting from the middle, or reordering is detected
(tested, including on an exported copy that anyone can re-verify). Refusals are audited too; an early version rolled the refusal record back
together with the failed request, which a test now guards. Audit payloads are redacted and carry identifiers, not document text.

**Limit.** Deleting only the *last* records is undetectable from the chain alone (a test documents this). Production fixes it by periodically
anchoring the head hash in immutable storage (Azure Blob immutability policy or a ledger table).

## 10. Standard-library HTTP API and SQLite

**Chosen.** `http.server` and SQLite, so the system has one dependency and the API is tested over real sockets here.

**Why.** "Does it actually run" is a primary criterion. **Cost:** no OpenAPI page, no async, SQLite is single-writer (so the demo
deployment is one replica). The service layer is framework-independent; moving to FastAPI + Postgres is mechanical.

## 11. Azure

**Intended deployment:** Azure Container Apps (image built in ACR), user-assigned managed identity, Azure OpenAI with Entra ID auth (no
keys), Entra authentication on the app to supply approver identity, Log Analytics for stdout logs. **Production substitutions:** Azure AI
Search for retrieval, Azure AI Document Intelligence for scans (the `read_image` hook), Azure Database for PostgreSQL for state,
immutable Blob storage for the audit anchor, Key Vault for secrets, Application Insights for traces.

**Honest status:** none of this was deployed or tested; I had no Azure subscription. The code paths that touch Azure are small,
isolated, and documented as unverified.

## What I would do next

1. Embeddings and a larger labeled query set built from real analyst searches.
2. OCR for scans and photos (Document Intelligence), then evidence from photos.
3. Replace the sample precedence rules and settlement formula with rules signed off by legal and finance, with a test per rule.
4. Postgres state, immutable audit anchoring, Entra group-based role directory, approval notifications.
5. A feedback loop: record when humans override a proposal, and use it as evaluation data.
