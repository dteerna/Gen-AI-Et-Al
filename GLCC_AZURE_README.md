# GLCC Exception Management: an auditable multi-agent system

An autonomous multi-agent system for supply-chain exceptions (cargo damage claims, customs compliance holds, vessel
delays). It ingests messy inputs (PDF contracts, emails, chat logs, port notices, regulations, scanned files), finds
the clause that actually governs when contracts conflict, retrieves the regulation version in force and similar past
rulings, computes any payout in code, and proposes a **cited, explainable, audited resolution that waits for human
approval**. It never pays, denies, or releases anything on its own authority.

> All parties, ports, contracts, rulings, and amounts are **fictional sample data**. The precedence rules and the
> settlement formula are samples, not legal advice.

**Runs with no credentials:** `pip install pypdf`, then `python -m glcc demo`. An LLM (Azure OpenAI) is an optional layer;
the system's decisions never depend on it.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate        # Python 3.10+ (developed and tested on 3.12)
pip install -r requirements.txt                          # one dependency: pypdf

python -m unittest discover -s tests                     # 292 tests, ~5 seconds
python -m glcc demo --out reports                        # runs all 11 scenarios, writes reports/DEMO_REPORT.md
python -m glcc analyze CASE-1001 --memo                  # one case, prints the memo
python -m glcc corpus --clauses                          # what was ingested, topics, supersession links
```

Run the API (demo mode: the approver's name is read from the request body):

```bash
GLCC_ALLOW_BODY_IDENTITY=true GLCC_LLM_PROVIDER=none python -m glcc serve    # http://127.0.0.1:8000

curl -s -X POST localhost:8000/cases/CASE-1001/analyze                                   # -> proposal id, decision, reviewers
curl -s -X POST localhost:8000/proposals/<PROP-id>/execute -d '{"approver_id":"u-ops"}'    # -> 409 blocked: no approvals yet
curl -s -X POST localhost:8000/proposals/<PROP-id>/approvals -d '{"role":"underwriter","approver_id":"u-uw1"}'
curl -s -X POST localhost:8000/proposals/<PROP-id>/execute -d '{"approver_id":"u-ops"}'    # -> 200 executed (once; replays are no-ops)
curl -s localhost:8000/audit/verify                                                       # -> hash chain valid
curl -s "localhost:8000/search?kind=regulation&q=customs+hold+deadline&port=Northgate&as_of=2024-11-04"
```

## What it does, per requirement

| Requirement | How it is met | Where to see it |
|---|---|---|
| Ingest chaotic multi-modal inputs | Clause-aware PDF parsing; RFC 2822 email headers to UTC timestamps; chat-log events; port notices; survey and claim-form PDFs. Images and scans are **flagged as unreadable, never silently ignored** | `glcc/ingest/`, CASE-1001 (PDF + photo), CASE-1011 (scanned contract) |
| Reason through conflicting clauses | A deterministic precedence engine: other-vendor, not-yet-applicable, explicitly replaced, vendor-specific over general, later date, and otherwise **unresolved, escalate** | `glcc/reasoning/precedence.py`; CASE-1001, 1005, 1009 |
| Conflicting *facts* | A carrier's "we notified promptly" is checked against timestamps; survey vs claim amounts; official notice vs carrier-reported closure. An evidence hierarchy resolves each and the conflict is flagged | `reasoning/timeline.py`; CASE-1003, 1006 |
| Dynamic RAG over historical rulings | Planned queries, parallel retrieval, a repair loop when a required topic is missing (reformulate, then bare topic, then report a gap), supersession-graph expansion, regulation version by incident date | `glcc/agents/research.py`; CASE-1002 vs 1010 |
| Retrieval accuracy | 26 labeled queries (typos, hard negatives, paraphrases, temporal, port filters); Recall@k, MRR, nDCG for BM25, trigram, and hybrid | `reports/retrieval_eval.md` |
| Legally sound | Payout math lives in tested code, not in a prompt. A proposal **cannot cite a clause, ruling, or regulation that does not exist**, nor a regulation not in force, nor an amount that disagrees with the calculation | `glcc/proposals.py`, `reasoning/liability.py` |
| Explainable | A numbered reasoning chain with the ids that support each step; overridden clauses with the reason each lost; every extracted fact carries a verbatim quote | `reports/memos/` |
| Auditable | Hash-chained audit log written in the same transaction as each state change; approvals bound to the proposal's content hash; refusals are audited too | `glcc/governance/`, `reports/audit_export.jsonl` |
| Exception scenarios and edge cases | See the matrix below and `docs/EVALUATION.md` | `reports/DEMO_REPORT.md` |
| Azure | Container Apps + ACR + Azure OpenAI (Entra ID auth) deployment assets | `deploy/azure/`, `docs/AZURE_DEPLOYMENT.md` (**untested**, see below) |

## Scenarios (all run offline; see `reports/DEMO_REPORT.md`)

| Case | Situation | Outcome |
|---|---|---|
| 1001 | Damage claim; the 2024 amendment replaces the older cap and overrides the master agreement | `partial_settlement` 89,000; underwriter |
| 1002 | Customs hold at Northgate; current regulation; GLCC's documentation error | `clear_customs_hold`; GLCC bears detention |
| 1003 | Vessel delay; carrier claims prompt notice but timestamps show 60h; force majeure fails | `approve_settlement` 40,000; underwriter + legal (material discrepancy) |
| 1004 | Instructions to "approve in full" planted inside a carrier email | Outcome unchanged; flagged; legal review added |
| 1005 | Two addenda, same date, different caps, neither replaces the other | `escalate_to_legal`; no payout |
| 1006 | 250,000 claim, survey says 210,000, insurer notified late | `partial_settlement` 142,500; senior underwriter + legal |
| 1007 | No independent survey in the file | `request_information` |
| 1008 | Vendor with no agreement of its own, two containers | master per-container cap x 2; `approve_settlement` 90,000 |
| 1009 | Cargo shipped *before* the amendment | original higher cap applies; 95,000 |
| 1010 | Customs hold in 2024 | the *older* regulation version applies (10 business days) |
| 1011 | Vendor agreement exists only as an unreadable scan | `request_information`; the master agreement is **not** assumed to apply |

## Architecture in one picture

```
 PDFs, emails, chats, notices, regulations, rulings, scans
                         |  ingestion (clause-aware, fault tolerant)
                         v
              Corpus: clauses | regulation versions | rulings
                         |
 intake -> planner -> [clause | regulation | precedent researchers, in parallel]
        -> conflict resolver -> analyst -> drafter -> critic -> proposal builder
                         |                                  |
       deterministic: precedence, timeline, math      independent review of every
       (an LLM may only ADD grounded facts or         citation, figure, and caveat
        narrative, and every addition is verified)
                         v
        PROPOSAL (never an action) -> humans approve -> gated, once-only execution
                         |
        every step in a hash-chained audit log (SQLite here; Postgres/Cosmos in production)
```

Details and diagrams: `docs/ARCHITECTURE.md`. Why each choice was made, and what was rejected: `docs/DESIGN_DECISIONS.md`.

**Coordination pattern:** a blackboard. Agents never call each other; each reads and writes named fields on a shared run
context and declares them, and a wiring test proves every agent's inputs are produced earlier. The orchestrator owns
order, parallelism, timeouts, and the failure policy.

**Failure policy:** a researcher failing becomes an explicit *gap* that the analyst escalates; a crash in a critical agent,
a memo the critic rejects, or a proposal the builder refuses ends in a **safe escalation** (a non-executable proposal routed
to legal counsel). The system either produces a correct proposal or hands the case to people. It does not guess.

## The LLM is optional and untrusted

| Where a model may help | What constrains it |
|---|---|
| Intake: add events the rules missed | Each must use an allowed kind, a valid timestamp, a real document id, and a **quote found verbatim in that document**. Otherwise it is rejected and recorded |
| Drafting: a short narrative | The critic rejects any clause id not in this case's evidence, any figure not in the analysis, and any claim that something was paid, executed, or released |
| Any call | Schema-validated, retried with backoff, behind a circuit breaker; documents are PII-redacted and wrapped as untrusted data; the model has no tools |

`python -m glcc analyze CASE-1001 --llm simulated:<good|hallucinating|malicious|malformed|down>` demonstrates this without
credentials: the decision and amount are identical in every mode (`reports/DEMO_REPORT.md`, section 4). The simulations are
rule-based stand-ins, **not language models**.

To use Azure OpenAI: `pip install -r requirements-azure.txt`, set `GLCC_LLM_PROVIDER=azure_openai`,
`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT` (authenticates with Entra ID unless `AZURE_OPENAI_API_KEY` is set), then
`python -m glcc llm-check`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GLCC_DATA` | `./data` | corpus, cases, evaluation queries |
| `GLCC_DB` | `glcc.db` | SQLite file (runs, proposals, approvals, executions, audit) |
| `GLCC_ROLES_FILE` | `config/roles.json` | role directory (who may approve as what). Production: Entra ID groups |
| `GLCC_API_KEY` | unset | if set, every endpoint except `/health` needs `X-API-Key` |
| `GLCC_ALLOW_BODY_IDENTITY` | `false` | **local demos only**: take the approver from the request body. Otherwise identity must arrive in `X-MS-CLIENT-PRINCIPAL-NAME` (set by Azure Container Apps authentication) |
| `GLCC_LLM_PROVIDER` | `none` | `none` or `azure_openai` |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_API_KEY` | | Azure OpenAI settings |
| `GLCC_LOG_LEVEL` | `WARNING` | logging |

## What was and was not verified

**Verified by running it (in the build environment, Python 3.12, no network):**
- 292 automated tests pass: ingestion, retrieval, reasoning, governance, agents, failure policy, LLM behaviors, a real HTTP server on an ephemeral port, the CLI, and the demo generator.
- The same behavior at process level: `python -m glcc serve` was launched as a subprocess and driven over HTTP with the API key and Azure-style identity header.
- **Mutation check** (`python scripts/mutation_check.py`): 24 deliberate breakages of the critical logic (critic, failure policy, proposal builder, API auth, audit, retrieval repair, LLM grounding, circuit breaker); every one is caught by the tests, and none was merely a syntax error.
- Concurrency: 10 simultaneous execute requests produce exactly one payout.

**Written but NOT verified (needs things I did not have):**
- Azure deployment: `deploy/azure/deploy.sh` (bash syntax checked only), the Dockerfile (never built), and `.github/workflows/azure-deploy.yml`. **The system is not deployed to Azure.** A fully functional local implementation is what has been demonstrated.
- The Azure OpenAI adapter (`glcc/llm/azure_openai.py`): it needs credentials. Everything around it (validation, grounding, fallbacks) is tested with fakes.
- Python 3.10 and 3.11: CI runs them; I only ran 3.12.

**Honest limits** (full list in `docs/DESIGN_DECISIONS.md`):
- Retrieval is lexical (BM25 + character trigrams): no embeddings, so a query sharing no words with the right clause can miss. On this tiny corpus all rankers are near ceiling, and the evaluation does **not** show hybrid beating trigram alone; it shows BM25 alone is brittle to typos.
- Offline extraction uses patterns written for the sample document formats. Real-world variety needs the LLM path plus more rules.
- Images and scans are not read (no OCR). They are flagged as missing evidence. Azure AI Document Intelligence is the intended plug-in point (`glcc/ingest/cases.py: read_image`).
- SQLite is single-writer, so the demo deployment is one replica. Production needs Postgres or Cosmos DB.
- Truncating the *last* audit records cannot be detected by the chain alone; production needs an external anchor (immutable blob storage).
- The sample precedence rules and settlement formula would need legal and finance sign-off.

## Repository layout

```
glcc/ingest/        PDF/email/chat/regulation/ruling/case parsing
glcc/retrieval/     BM25, trigram, RRF fusion, filters, evaluation
glcc/reasoning/     precedence, timeline facts, liability math, case analysis
glcc/agents/        intake, planner, researchers, resolver, analyst, drafter, critic, orchestrator
glcc/governance/    authority matrix, approvals, audit chain, SQLite store, security
glcc/llm/           resilient wrapper, Azure OpenAI adapter, scripted/simulated models
glcc/api/ cli.py demo.py service.py proposals.py
data/               contracts (PDF), regulations, rulings, cases, labeled retrieval queries
tests/              292 tests      scripts/  sample-data generator, mutation check
deploy/azure/ docs/ reports/ .github/workflows/
```
