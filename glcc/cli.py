"""Command-line interface. `python -m glcc --help`"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import config
from .corpus import Corpus
from .governance.store import Store, StoreError
from .ingest.common import IngestError
from .llm import LLMError, ResilientLLM, llm_from_env
from .llm.fake import MODES, SimulatedLLM
from .service import ExceptionService


def _llm(spec: str):
    if spec in ("none", ""):
        return None
    if spec == "env":
        return llm_from_env()
    if spec.startswith("simulated:") and spec.split(":", 1)[1] in MODES:
        return ResilientLLM(SimulatedLLM(spec.split(":", 1)[1]), sleep=lambda s: None, max_retries=1)
    raise SystemExit(f"--llm must be none, env, or simulated:<{'|'.join(MODES)}>")


def _service(args) -> ExceptionService:
    try:
        corpus = Corpus.load(args.data or config.data_dir())
    except IngestError as exc:
        raise SystemExit(f"error: {exc}")
    return ExceptionService(corpus, Store(args.db or config.db_path()), _llm(getattr(args, "llm", "none")), config.load_directory())


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_corpus(args):
    svc = _service(args)
    c = svc.corpus
    print("stats:", json.dumps(c.stats()))
    for w in c.warnings:
        print("warning:", w)
    print("cases:", ", ".join(c.case_ids()))
    if args.clauses:
        for cl in c.clauses:
            if cl.topic != "other":
                sup = f" replaces {','.join(cl.supersedes)}" if cl.supersedes else ""
                print(f"  {cl.clause_id:26} {cl.topic:22} {cl.scope:15} applies from {cl.applies_from}{sup}  {cl.terms}")


def cmd_analyze(args):
    svc = _service(args)
    r = svc.analyze(args.case_id, actor="cli")
    if args.json:
        return _print({k: v for k, v in r.items() if k != "audit_events"})
    s = svc.summarize_run(r)
    print(f"{r['case_id']}: {r['status']}  decision={r['decision']}  amount={r['amount']}")
    if r["error"]:
        print("error:", r["error"])
    if r["proposal"]:
        print(f"proposal {r['proposal']['proposal_id']}  required approvals: {', '.join(s['required_approvals'])}")
    print("flags:", s["flags"] or "none", "| blockers:", s["blockers"] or "none")
    print("agents:", ", ".join(f"{a['agent']}[{a['status']}]" for a in s["agents"]))
    if args.memo and r["memo"]:
        print("\n" + r["memo"])
    if r["status"] == "failed":
        sys.exit(2)


def cmd_proposals(args):
    for p in _service(args).store.list_proposals():
        c = p["content"]
        print(f"{p['proposal_id']}  {c['case_id']}  {c['decision_type']:19} {c['amount']:>11,.2f}  {p['status']:17} needs {', '.join(c['required_approvals'])}")


def _guard(fn):
    try:
        return fn()
    except StoreError as exc:
        print("refused:", exc)
        sys.exit(1)


def cmd_approve(args):
    _print(_guard(lambda: _service(args).approve(args.proposal_id, args.role, args.approver)))


def cmd_revoke(args):
    _print(_guard(lambda: _service(args).revoke(args.approval_id, args.approver)))


def cmd_execute(args):
    r = _guard(lambda: _service(args).execute(args.proposal_id, f"user:{args.approver}"))
    _print(r)
    if r["status"] != "executed":
        sys.exit(1)


def cmd_audit(args):
    svc = _service(args)
    if args.verify:
        v = svc.verify_audit()
        _print(v)
        sys.exit(0 if v["valid"] else 1)
    for r in svc.store.audit_records(limit=args.tail):
        print(f"{r['seq']:>4} {r['ts'][:19]} {r['actor']:18} {r['event']:24} {r['case_id'] or '':10} {json.dumps(r['payload'], sort_keys=True)[:90]}")


def cmd_search(args):
    for r in _service(args).search(args.kind, args.query, args.vendor, args.port, args.as_of):
        if "excluded" in r:
            print("  (excluded)", r["excluded"]["id"], "-", r["excluded"]["reason"])
        else:
            rid = r.get("clause_id") or r.get("section_id") or r.get("ruling_id")
            print(f"  #{r['rank']} {rid:28} score={r['score']:.4f} bm25_rank={r['bm25_rank']} trigram_rank={r['trigram_rank']}  {r['text'][:70] if 'text' in r else r.get('summary', '')[:70]}")


def cmd_retrieval_eval(args):
    from .retrieval.evaluation import RetrievalEvaluator, load_queries, render_markdown
    corpus = Corpus.load(args.data or config.data_dir())
    ev = RetrievalEvaluator(corpus)
    qs = load_queries(Path(args.data or config.data_dir()) / "retrieval_eval.json")
    text = render_markdown([ev.run(qs, 3), ev.run(qs, 5)])
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print("wrote", args.out)
    else:
        print(text)


def cmd_serve(args):
    from .api.server import make_server
    svc = _service(args)
    key = os.getenv("GLCC_API_KEY") or None
    allow_body = os.getenv("GLCC_ALLOW_BODY_IDENTITY", "false").lower() == "true"
    if not key:
        print("WARNING: GLCC_API_KEY is not set; the API is unauthenticated. Fine for local demos only.", file=sys.stderr)
    if allow_body:
        print("WARNING: GLCC_ALLOW_BODY_IDENTITY=true; approver identity is taken from the request body. Local demos only.", file=sys.stderr)
    server = make_server(svc, args.host, args.port, key, allow_body)
    print(f"serving on http://{args.host}:{args.port}  mode={'llm' if svc.llm else 'offline'}  db={args.db or config.db_path()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


def cmd_demo(args):
    from .demo import build_demo
    out = build_demo(args.out, args.data or config.data_dir())
    print(f"{'case':10} {'status':10} {'decision':20} {'amount':>12}")
    for cid, r in out["results"].items():
        print(f"{cid:10} {r['status']:10} {r['decision']:20} {r['amount']:>12,.2f}")
    print(f"\naudit chain valid: {out['chain']['valid']} ({out['chain']['records']} records)")
    print("report:", out["report"])


def cmd_llm_check(args):
    try:
        llm = llm_from_env()
    except LLMError as exc:
        raise SystemExit(f"error: {exc}")
    if llm is None:
        raise SystemExit("GLCC_LLM_PROVIDER is 'none'. Set GLCC_LLM_PROVIDER=azure_openai plus AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT.")
    out = llm.complete_json(system="Reply with JSON only.", user='Return {"ok": true}.', schema={"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}})
    print("LLM reachable and returned valid JSON:", out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="glcc", description="Auditable multi-agent logistics exception management")
    p.add_argument("--db", help="SQLite path (default $GLCC_DB or glcc.db)")
    p.add_argument("--data", help="data directory (default $GLCC_DATA or ./data)")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    s = add("corpus", cmd_corpus, "show what was ingested")
    s.add_argument("--clauses", action="store_true")
    s = add("analyze", cmd_analyze, "analyze a case and create a proposal")
    s.add_argument("case_id")
    s.add_argument("--llm", default="none", help="none | env | simulated:<good|hallucinating|malicious|malformed|down>")
    s.add_argument("--memo", action="store_true", help="print the memo")
    s.add_argument("--json", action="store_true", help="print the full run as JSON")
    add("proposals", cmd_proposals, "list proposals")
    s = add("approve", cmd_approve, "record a human approval")
    s.add_argument("proposal_id"); s.add_argument("--role", required=True); s.add_argument("--as", dest="approver", required=True)
    s = add("revoke", cmd_revoke, "revoke your own approval")
    s.add_argument("approval_id", type=int); s.add_argument("--as", dest="approver", required=True)
    s = add("execute", cmd_execute, "execute an approved proposal (stub action)")
    s.add_argument("proposal_id"); s.add_argument("--as", dest="approver", default="cli")
    s = add("audit", cmd_audit, "show or verify the audit log")
    s.add_argument("--verify", action="store_true"); s.add_argument("--tail", type=int, default=30)
    s = add("search", cmd_search, "search clauses, regulations, or rulings")
    s.add_argument("query"); s.add_argument("--kind", default="clause", choices=["clause", "regulation", "ruling"])
    s.add_argument("--vendor", default="any"); s.add_argument("--port", default="any"); s.add_argument("--as-of", default="2025-02-03")
    s = add("retrieval-eval", cmd_retrieval_eval, "evaluate retrieval on the labeled queries")
    s.add_argument("--out")
    s = add("serve", cmd_serve, "run the HTTP API")
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8000)
    s.add_argument("--llm", default="env", help="none | env | simulated:<mode>")
    s = add("demo", cmd_demo, "run every scenario and write reports/")
    s.add_argument("--out", default="reports")
    add("llm-check", cmd_llm_check, "verify Azure OpenAI configuration")
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=os.getenv("GLCC_LOG_LEVEL", "WARNING"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except IngestError as exc:
        raise SystemExit(f"error: {exc}")
    except LLMError as exc:
        raise SystemExit(f"LLM error: {exc}")


if __name__ == "__main__":
    main()
