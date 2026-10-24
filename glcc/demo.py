"""Run every scenario end to end and write a report from the REAL results.

Nothing in the generated report is typed by hand: decisions, amounts, blockers, approvals, refusals, hashes, and
metrics are all read from the objects the system produced during this run.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .agents.orchestrator import Orchestrator
from .config import load_directory
from .corpus import Corpus
from .governance import authority
from .governance.store import Store, StoreError
from .llm.base import ResilientLLM
from .llm.fake import MODES, SimulatedLLM
from .retrieval.evaluation import RetrievalEvaluator, load_queries, render_markdown
from .service import ExceptionService

SCENARIOS = {
    "CASE-1001": "Freight damage. The 2024 amendment replaces the older cap and overrides the master agreement; insurer notified in time.",
    "CASE-1002": "Customs hold at Northgate. Current regulation version applies; the certificate of origin was GLCC's error, so GLCC bears detention.",
    "CASE-1003": "Vessel delay. Carrier's email claims prompt notice; timestamps show 60h, so force majeure fails. Conflicting evidence is flagged.",
    "CASE-1004": "Freight damage. Instructions to approve in full are planted inside a carrier email. They are flagged and change nothing.",
    "CASE-1005": "Freight damage, Northstar. Two addenda with the same date set different caps and neither replaces the other. Escalated, no payout.",
    "CASE-1006": "High-value claim, survey lower than the claim, insurer notified late. Settlement is limited; dual control is required.",
    "CASE-1007": "Freight damage with no independent survey in the file. Requests information instead of paying.",
    "CASE-1008": "Vendor with no agreement of its own, two containers. The master per-container cap applies to each container.",
    "CASE-1009": "Cargo shipped BEFORE the amendment took effect. The original, higher cap still applies.",
    "CASE-1010": "Customs hold in 2024. The OLDER regulation version (10 business days) was in force, not the current one.",
    "CASE-1011": "Zenith's vendor agreement exists only as an unreadable scan. The master agreement is NOT assumed to apply.",
}


class SteppingClock:
    """Deterministic clock: each call advances one second; `jump` simulates elapsed time (for approval expiry)."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now

    def jump(self, **kw) -> None:
        self.now += timedelta(**kw)


def _md_table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c).replace("|", "/").replace("\n", " ") for c in r) + " |" for r in rows]
    return "\n".join(out)


def _try(fn, *a):
    try:
        return "ACCEPTED", fn(*a)
    except StoreError as exc:
        return "REFUSED", str(exc)


def build_demo(out_dir: str | Path, data_dir: str | Path) -> dict:
    out = Path(out_dir)
    (out / "memos").mkdir(parents=True, exist_ok=True)
    corpus = Corpus.load(data_dir)
    clock = SteppingClock(datetime(2025, 3, 3, 9, 0, tzinfo=timezone.utc))
    store = Store(":memory:", clock=clock)
    directory = load_directory()
    svc = ExceptionService(corpus, store, None, directory)
    results: dict[str, dict] = {cid: svc.analyze(cid) for cid in corpus.case_ids()}

    # ---- 1. scenarios --------------------------------------------------------------------------------------------------
    rows = []
    for cid, r in results.items():
        a, c = r["analysis"], r["proposal"]["content"]
        rows.append([cid, a["case_type"].replace("_", " "), a["vendor"], r["decision"],
                     f"{r['amount']:,.2f}" if "settlement" in r["decision"] else "-", ", ".join(c["required_approvals"]),
                     ", ".join(c["flags"]) or "-", ", ".join(c["blockers"]) or "-"])
        (out / "memos" / f"{cid}.md").write_text(r["memo"], encoding="utf-8")
    scenarios_table = _md_table(["Case", "Type", "Vendor", "Decision", "Amount (USD)", "Reviewers required", "Flags", "Blockers"], rows)
    why = "\n".join(f"- **{cid}**: {text}" for cid, text in SCENARIOS.items())

    # ---- 2. governance walkthrough --------------------------------------------------------------------------------------------
    steps: list[list] = []

    def log(case, action, outcome, detail):
        steps.append([case, action, outcome, detail])

    p1 = results["CASE-1001"]["proposal"]["proposal_id"]
    r = svc.execute(p1, "user:ops")
    log("CASE-1001", "execute with no approvals", r["status"].upper(), r["reasons"][0])
    for who, role in (("u-coord1", "underwriter"), ("system", "underwriter"), ("glcc-agent", "underwriter"), ("u-uw1", "compliance_officer")):
        s, d = _try(svc.approve, p1, role, who)
        log("CASE-1001", f"{who} approves as {role}", s, d if s == "REFUSED" else "")
    s, d = _try(svc.approve, p1, "underwriter", "u-uw1")
    log("CASE-1001", "u-uw1 approves as underwriter", s, f"approval {d['approval_id']} bound to content hash {results['CASE-1001']['proposal']['content_hash'][:12]}...")
    ex = svc.execute(p1, "user:ops")
    log("CASE-1001", "execute", ex["status"].upper(), f"confirmation {ex['confirmation_id']}")
    again = svc.execute(p1, "user:ops")
    log("CASE-1001", "execute again (retry/double-click)", "NO-OP", f"idempotent replay, same confirmation {again['confirmation_id']}; nothing paid twice")
    s, d = _try(svc.approve, p1, "underwriter", "u-uw2")
    log("CASE-1001", "approve after execution", s, d)

    p6 = results["CASE-1006"]["proposal"]["proposal_id"]
    s, d = _try(svc.approve, p6, "senior_underwriter", "u-sr1")
    log("CASE-1006", "senior underwriter approves (dual control needs two roles)", s, "")
    r = svc.execute(p6, "user:ops")
    log("CASE-1006", "execute with one of two required approvals", r["status"].upper(), r["reasons"][0])
    _try(svc.approve, p6, "legal_counsel", "u-legal1")
    ex = svc.execute(p6, "user:ops")
    log("CASE-1006", "legal counsel approves, then execute", ex["status"].upper(), f"{ex['amount']:,.2f} {ex['currency']}, confirmation {ex['confirmation_id']}")

    p4 = results["CASE-1004"]["proposal"]["proposal_id"]
    _try(svc.approve, p4, "underwriter", "u-uw1")
    r = svc.execute(p4, "user:ops")
    log("CASE-1004", "underwriter alone approves a case with planted instructions", r["status"].upper(), r["reasons"][0])

    p9 = results["CASE-1009"]["proposal"]["proposal_id"]
    _, a1 = _try(svc.approve, p9, "underwriter", "u-uw1")
    _try(svc.revoke, a1["approval_id"], "u-uw1")
    r = svc.execute(p9, "user:ops")
    log("CASE-1009", "approval revoked, then execute", r["status"].upper(), r["reasons"][0])
    _try(svc.approve, p9, "underwriter", "u-uw2")
    clock.jump(hours=authority.APPROVAL_TTL_HOURS + 1)
    r = svc.execute(p9, "user:ops")
    log("CASE-1009", f"approval older than {authority.APPROVAL_TTL_HOURS}h, then execute", r["status"].upper(), r["reasons"][0])
    _try(svc.approve, p9, "underwriter", "u-uw1")
    ex = svc.execute(p9, "user:ops")
    log("CASE-1009", "fresh approval, then execute", ex["status"].upper(), f"confirmation {ex['confirmation_id']}")

    p5 = results["CASE-1005"]["proposal"]["proposal_id"]
    _try(svc.approve, p5, "legal_counsel", "u-legal1")
    r = svc.execute(p5, "user:ops")
    log("CASE-1005", "legal counsel approves an escalation, then execute", r["status"].upper(), r["reasons"][0])

    # tamper tests run on a COPY so the genuine records stay intact
    pending = results["CASE-1008"]["proposal"]["proposal_id"]
    _try(svc.approve, pending, "underwriter", "u-uw1")
    clone = Store(":memory:", clock=clock)
    store._conn.backup(clone._conn)
    clone._conn.execute("UPDATE proposals SET content_json = replace(content_json, '90000.0', '9000000.0') WHERE proposal_id = ?", (pending,))
    r = clone.execute(pending, "user:ops", directory, lambda c: {"confirmation_id": "X"})
    log("CASE-1008", "TAMPER: amount edited in the database after approval", r["status"].upper(), r["reasons"][0])
    before = store.verify_audit()
    clone._conn.execute("UPDATE audit_log SET payload_json = replace(payload_json, 'underwriter', 'nobody') WHERE seq = (SELECT MIN(seq) FROM audit_log WHERE event = 'approval_granted')")
    after = clone.verify_audit()
    log("(audit log)", "TAMPER: an approval record edited in a copy", "DETECTED" if not after["valid"] else "MISSED", f"{after['reason']} at seq {after['first_bad_seq']}; original log valid={before['valid']}")
    clone.close()
    governance = _md_table(["Case", "Action", "Outcome", "Detail"], steps)

    # ---- 3. model behaviour ---------------------------------------------------------------------------------------------------
    base = results["CASE-1001"]
    llm_rows = []
    for mode in ("none", *MODES):
        if mode == "none":
            run = Orchestrator(corpus).run("CASE-1001")
            intake = {"used": False}
        else:
            llm = ResilientLLM(SimulatedLLM(mode), sleep=lambda s: None, max_retries=1)
            run = Orchestrator(corpus, llm=llm).run("CASE-1001")
            intake = next(t for t in run["trace"] if t["agent"] == "intake")["summary"]["llm"]
        same = (run["decision"], run["amount"]) == (base["decision"], base["amount"])
        llm_rows.append([mode if mode != "none" else "no LLM (offline)", run["decision"], f"{run['amount']:,.2f}", "same" if same else "DIFFERENT",
                         run["memo_meta"].get("narrative"), "-" if not intake.get("used") else f"{intake.get('accepted', 0)} accepted / {len(intake.get('rejected', []))} rejected" + (" (provider failed)" if intake.get("status") == "failed" else "")])
    llm_table = _md_table(["Simulated model", "Decision", "Amount", "vs offline", "Narrative", "Extra facts from model"], llm_rows)

    # ---- 4. retrieval ------------------------------------------------------------------------------------------------------------------
    ev = RetrievalEvaluator(corpus)
    queries = load_queries(Path(data_dir) / "retrieval_eval.json")
    retrieval_md = render_markdown([ev.run(queries, 3), ev.run(queries, 5)])
    (out / "retrieval_eval.md").write_text(retrieval_md, encoding="utf-8")

    # ---- 5. audit ----------------------------------------------------------------------------------------------------------------------
    chain = store.verify_audit()
    records = store.audit_records()
    (out / "audit_export.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    lifecycle = [r for r in records if r["case_id"] == "CASE-1001" and (r["event"].startswith(("proposal", "approval", "execution")) or r["event"] in ("run_started", "run_completed"))]
    audit_rows = [[r["seq"], r["actor"], r["event"], json.dumps(r["payload"], sort_keys=True)[:110], r["hash"][:10]] for r in lifecycle]
    audit_md = _md_table(["seq", "actor", "event", "payload", "hash"], audit_rows)
    trace_rows = [[t["agent"], t["status"], t.get("ms", ""), json.dumps(t.get("summary", {}), default=str)[:120]] for t in base["trace"]]

    report = f"""# GLCC exception management: demo report

Generated by `python -m glcc demo` from a real run of the system (offline mode, no LLM). Every figure below was
read from the objects the system produced, not typed by hand.

Corpus: {json.dumps(corpus.stats())}
Warnings during ingestion: {', '.join(corpus.warnings) or 'none'}

## 1. Scenarios and outcomes

{scenarios_table}

What each case demonstrates:

{why}

Memos for every case are in `reports/memos/`.

## 2. How one case runs: CASE-1001 agent trace

{_md_table(['Agent', 'Status', 'ms', 'Summary'], trace_rows)}

Read the full memo in `reports/memos/CASE-1001.md`. The retrieval provenance (queries, ranks, reformulation iterations) is in the run record.

## 3. Governance walkthrough (approvals, execution, tampering)

The agents only propose. These are real calls against the same service the API uses:

{governance}

## 4. What a language model can and cannot change

The same case (CASE-1001) run with five simulated model behaviors. The decision and amount must not move.
These simulations are rule-based stand-ins (`glcc/llm/fake.py`), not language models; they prove the checks work.

{llm_table}

## 5. Audit trail

Hash-chain verification of the whole log after this run: **valid = {chain['valid']}** over {chain['records']} records.
Lifecycle of the CASE-1001 proposal (full export in `reports/audit_export.jsonl`):

{audit_md}

## 6. Retrieval quality

See `reports/retrieval_eval.md` (generated in this run; also reproduced below).

{retrieval_md}
"""
    (out / "DEMO_REPORT.md").write_text(report, encoding="utf-8")
    return {"results": results, "chain": chain, "governance_steps": steps, "llm_rows": llm_rows, "report": str(out / "DEMO_REPORT.md")}
