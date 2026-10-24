# Architecture

## 1. Components

```mermaid
flowchart LR
  subgraph IN[Inputs]
    PDF[Contract PDFs]
    EML[Emails .eml]
    CHT[Chat logs]
    NOT[Port notices]
    REG[Regulation text]
    RUL[Historical rulings]
    IMG[Images and scans]
  end
  IN --> ING[Ingestion<br/>clause-aware, fault tolerant]
  ING --> KB[(Corpus<br/>clauses, regulation versions,<br/>rulings, vendors)]
  KB --> RET[Hybrid retrieval<br/>BM25 + trigram + RRF<br/>vendor, port, in-force filters]
  RET --> ORCH
  subgraph ORCH[Orchestrator: blackboard pipeline]
    A1[intake] --> A2[planner] --> R[3 researchers<br/>in parallel] --> A3[conflict resolver] --> A4[analyst] --> A5[drafter] --> A6[critic] --> A7[proposal builder]
  end
  LLM[(Optional LLM<br/>Azure OpenAI)] -. adds grounded facts<br/>and narrative only .-> ORCH
  ORCH --> STORE[(SQLite store<br/>runs, proposals, approvals,<br/>executions)]
  ORCH --> AUD[(Hash-chained<br/>audit log)]
  STORE --> API[HTTP API / CLI]
  API --> HUM[Humans approve<br/>per authority matrix]
  HUM --> GATE{Execution gate<br/>once only}
  GATE --> ACT[Payment / customs action<br/>stub]
  GATE --> AUD
```

## 2. The pipeline and what each stage owns

| Stage | Kind | Reads | Writes | Notes |
|---|---|---|---|---|
| `intake` | critical | case folder | `case`, `facts` | Deterministic extraction is the baseline; an LLM may add events that pass grounding checks |
| `planner` | critical | `case`, `facts` | `plan` | Chooses topics and queries from the case type, the vendor's documents, and detected issues |
| `clause_researcher` | non-critical | `case`, `plan` | `retrieved.clauses`, gaps | Repair loop: plain query, then synonym expansion, then bare topic, then a reported gap |
| `regulation_researcher` | non-critical | `case`, `facts`, `plan` | `retrieved.regulations` | Only the version **in force on the incident date**; reports what it excluded and why |
| `precedent_researcher` | non-critical | `case`, `plan` | `retrieved.rulings` | Similar past rulings, vendor boost, lexical-overlap floor (no filler precedents) |
| `conflict_resolver` | critical | clauses | `topic_rulings` | Expands the set along "replaces" links, then applies the precedence rules |
| `analyst` | critical | everything above | `analysis` | Checks, payout math, decision, blockers, reasoning chain |
| `drafter` | critical | `analysis` | `memo` | Deterministic memo; optional LLM narrative, verified |
| `critic` | critical | `analysis`, `memo` | `memo_meta` | Independent review; drops an LLM narrative that fails |
| `proposal_builder` | | `analysis`, `memo` | `proposal` | Refuses invented citations, stale regulations, mismatched amounts |

The three researchers run concurrently and cannot read each other's output (a test enforces it). Each agent declares `reads` and
`writes`; `tests/test_agents.py::TestWiring` proves every read is produced by an earlier stage.

## 3. Trust boundaries

```mermaid
flowchart TB
  subgraph UNTRUSTED[Untrusted input]
    D[Case documents<br/>written by third parties]
    M[LLM output]
  end
  subgraph CHECKS[Verification layer]
    G[Grounding: quote must appear verbatim in the cited document]
    C[Critic: ids exist and belong to this case, figures traced to the analysis,<br/>no claim that anything was executed]
    S[Injection scan: flags instruction-like text]
  end
  subgraph TRUSTED[Deterministic core: decides]
    P[Precedence engine]
    L[Settlement math]
    A[Authority matrix]
    X[Execution gate]
  end
  D --> S --> G --> P
  M --> G
  M --> C
  P --> L --> A --> X
  H[Authenticated humans] --> X
```

Nothing in the untrusted boxes can reach the decision logic except as a *fact that has passed a check*. The model has no
tools and no side effects. A planted instruction in an email (CASE-1004) is flagged, treated as data, and *adds* a reviewer.

## 4. Data model

```mermaid
erDiagram
  CLAUSE ||--o{ CLAUSE : "replaces"
  CLAUSE { string clause_id "VA-ACME-2022-AMD2#1.1" string topic string scope date effective_date date applies_from json terms }
  REG_SECTION { string section_id "PAN-REG-003.v2#2" int version date effective_date date valid_until json terms }
  RULING { string ruling_id string case_type string outcome json clauses_applied }
  RUN ||--o| PROPOSAL : produces
  PROPOSAL ||--o{ APPROVAL : "needs"
  PROPOSAL ||--o| EXECUTION : "at most one"
  RUN { string run_id string case_id json trace json analysis text memo }
  PROPOSAL { string proposal_id "PROP-" string content_hash json content "decision, amount, citations, required_approvals" }
  APPROVAL { int approval_id string role string approver_id string content_hash datetime revoked_at }
  EXECUTION { string proposal_id "PRIMARY KEY" json result }
  AUDIT { int seq string prev_hash string hash string event json payload }
```

* A **clause id** is the document id plus the clause number as printed in the PDF, so a reviewer can find it on the page.
* The **proposal id** is derived from the SHA-256 of the proposal content. Identical content gives the identical id; any edit gives a
  different one and voids earlier approvals.
* `EXECUTION.proposal_id` is the primary key: at most one execution per proposal, even under races.

## 5. Approval and execution

```mermaid
sequenceDiagram
  participant Agent as Agents
  participant Store
  participant Human as Approver (Entra ID)
  participant Gate as Execution gate
  Agent->>Store: save proposal (content hash H, required roles R)
  Store-->>Agent: PROP-xxxx (audit: proposal_created)
  Human->>Store: approve as role r (identity from the authenticated header)
  Store->>Store: role held in directory? not a system identity? not the creator? (audit either way)
  Human->>Gate: execute PROP-xxxx
  Gate->>Gate: content hash == H ? decision executable? not executed?
  Gate->>Gate: every role in R has a valid approval: bound to H, unrevoked, under 72h,<br/>approver still holds the role, all approvers different people
  alt all checks pass
    Gate->>Store: run action + record execution + audit, in ONE transaction
    Gate-->>Human: executed (confirmation id)
  else any check fails
    Gate->>Store: audit execution_blocked (reasons)
    Gate-->>Human: blocked (reasons)
  end
```

Authority matrix (USD): settlement up to 10,000 needs a logistics coordinator; up to 100,000 an underwriter; above that a senior
underwriter **and** legal counsel; denial needs an underwriter; customs release needs a compliance officer. Risk flags
(`injection_suspected`, `material_discrepancy`) **add** legal counsel and can never remove a reviewer.

## 6. Failure handling

| Failure | Result |
|---|---|
| Unknown or malformed case | Run **fails**; nothing proposed; `run_failed` audited |
| A researcher crashes or times out | Recorded as a gap; the analyst escalates to legal; a precedent failure only loses precedents |
| A required clause topic cannot be found | Reformulate twice, then a reported gap and escalation |
| A vendor agreement exists but is unreadable | `request_information`; the master agreement is not assumed to apply |
| Two equally authoritative clauses conflict | `escalate_to_legal`; no payout computed |
| A critical agent raises, the critic rejects the memo, or the proposal builder refuses | **Safe escalation**: a non-executable proposal to legal counsel with the reason |
| LLM down, malformed, hallucinating, or malicious | Retried with backoff, then the deterministic path; the circuit breaker stops hammering a failing provider |
| Execution action fails | Nothing is recorded as executed; the failure is audited; retry is safe |
| Two execute requests race | One wins; the other returns the stored result (`idempotent_replay`) |
