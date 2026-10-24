"""SQLite persistence for runs, proposals, approvals, executions, and the audit log.

Why SQLite: zero setup, transactional, and enough to demonstrate the guarantees that matter:
  * a state change and its audit record commit together or not at all;
  * an execution row has a UNIQUE key on the proposal id, so a payout can happen once even if two
    requests race;
  * approvals are bound to the proposal content hash.
For production on Azure, swap this class for Azure Database for PostgreSQL or Cosmos DB behind the
same method names; see docs/DESIGN_DECISIONS.md.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import audit, authority
from .security import redact_obj

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
    mode TEXT NOT NULL, status TEXT NOT NULL, decision TEXT, proposal_id TEXT,
    trace_json TEXT, analysis_json TEXT, memo TEXT);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, content_json TEXT NOT NULL, content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL, run_id TEXT);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT NOT NULL, content_hash TEXT NOT NULL,
    role TEXT NOT NULL, approver_id TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT, revoked_by TEXT);
CREATE TABLE IF NOT EXISTS executions (
    proposal_id TEXT PRIMARY KEY, executed_at TEXT NOT NULL, executed_by TEXT NOT NULL, result_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL, run_id TEXT, case_id TEXT,
    payload_json TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
"""


class StoreError(Exception):
    """A request the store refuses (unknown proposal, forbidden approval, ...)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path = ":memory:", clock: Callable[[], datetime] | None = None):
        self.path = str(path)
        self._clock = clock or authority.utcnow
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---- transactions ---------------------------------------------------------------------------------------------
    def _tx(self):
        store = self

        class _Tx:
            def __enter__(self_inner):
                store._lock.acquire()
                store._conn.execute("BEGIN IMMEDIATE")
                return store._conn

            def __exit__(self_inner, exc_type, exc, tb):
                try:
                    store._conn.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    store._lock.release()
                return False

        return _Tx()

    def _audit(self, conn: sqlite3.Connection, actor: str, event: str, payload: dict, run_id: str | None = None,
               case_id: str | None = None) -> dict:
        row = conn.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        prev, seq = (row["hash"], row["seq"] + 1) if row else (audit.GENESIS, 1)
        rec = audit.make_record(prev, seq, self._clock().isoformat(), actor, event, redact_obj(payload), run_id, case_id)
        conn.execute("INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?)",
                     (rec["seq"], rec["ts"], rec["actor"], rec["event"], rec["run_id"], rec["case_id"],
                      json.dumps(rec["payload"], sort_keys=True, separators=(",", ":"), default=str), rec["prev_hash"], rec["hash"]))
        return rec

    def append_audit(self, actor: str, event: str, payload: dict, run_id: str | None = None, case_id: str | None = None) -> dict:
        with self._tx() as conn:
            return self._audit(conn, actor, event, payload, run_id, case_id)

    def audit_records(self, limit: int | None = None, run_id: str | None = None, case_id: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM audit_log", []
        clauses = []
        if run_id:
            clauses.append("run_id = ?"); args.append(run_id)
        if case_id:
            clauses.append("case_id = ?"); args.append(case_id)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY seq"
        rows = self._conn.execute(q, args).fetchall()
        recs = [self._row_to_audit(r) for r in rows]
        return recs[-limit:] if limit else recs

    @staticmethod
    def _row_to_audit(r: sqlite3.Row) -> dict:
        return {"seq": r["seq"], "ts": r["ts"], "actor": r["actor"], "event": r["event"], "run_id": r["run_id"], "case_id": r["case_id"],
                "payload": json.loads(r["payload_json"]), "prev_hash": r["prev_hash"], "hash": r["hash"]}

    def verify_audit(self) -> dict:
        return audit.verify_chain(self.audit_records())

    # ---- runs --------------------------------------------------------------------------------------------------------------
    def save_run(self, run: dict) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run["run_id"], run["case_id"], run["started_at"], run.get("finished_at"), run["mode"], run["status"],
                 run.get("decision"), run.get("proposal_id"), json.dumps(run.get("trace", []), default=str),
                 json.dumps(run.get("analysis"), default=str), run.get("memo")))

    def get_run(self, run_id: str) -> dict | None:
        r = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not r:
            return None
        return {**{k: r[k] for k in ("run_id", "case_id", "started_at", "finished_at", "mode", "status", "decision", "proposal_id", "memo")},
                "trace": json.loads(r["trace_json"] or "[]"), "analysis": json.loads(r["analysis_json"] or "null")}

    def list_runs(self, case_id: str | None = None) -> list[dict]:
        q = "SELECT run_id, case_id, started_at, status, decision, proposal_id, mode FROM runs"
        rows = self._conn.execute(q + (" WHERE case_id = ?" if case_id else "") + " ORDER BY started_at", (case_id,) if case_id else ()).fetchall()
        return [dict(r) for r in rows]

    # ---- proposals -----------------------------------------------------------------------------------------------------------
    def save_proposal(self, proposal: dict, run_id: str | None, actor: str) -> tuple[dict, bool]:
        """Idempotent: the id is derived from the content hash, so the same content yields the same proposal."""
        with self._tx() as conn:
            existing = conn.execute("SELECT proposal_id FROM proposals WHERE proposal_id = ?", (proposal["proposal_id"],)).fetchone()
            if existing:
                self._audit(conn, actor, "proposal_reused", {"proposal_id": proposal["proposal_id"]}, run_id, proposal["content"]["case_id"])
                return self.get_proposal(proposal["proposal_id"]), False
            conn.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?,?)",
                         (proposal["proposal_id"], proposal["content"]["case_id"], json.dumps(proposal["content"], sort_keys=True, default=str),
                          proposal["content_hash"], proposal["created_by"], self._clock().isoformat(), run_id))
            c = proposal["content"]
            self._audit(conn, actor, "proposal_created",
                        {"proposal_id": proposal["proposal_id"], "content_hash": proposal["content_hash"], "decision_type": c["decision_type"],
                         "amount": c["amount"], "required_approvals": c["required_approvals"], "flags": c["flags"]}, run_id, c["case_id"])
        return self.get_proposal(proposal["proposal_id"]), True

    def get_proposal(self, proposal_id: str) -> dict | None:
        r = self._conn.execute("SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
        if not r:
            return None
        ex = self._conn.execute("SELECT * FROM executions WHERE proposal_id = ?", (proposal_id,)).fetchone()
        return {"proposal_id": r["proposal_id"], "content_hash": r["content_hash"], "content": json.loads(r["content_json"]),
                "created_by": r["created_by"], "created_at": r["created_at"], "run_id": r["run_id"],
                "status": "executed" if ex else "pending_approval", "execution": json.loads(ex["result_json"]) if ex else None}

    def list_proposals(self) -> list[dict]:
        return [self.get_proposal(r["proposal_id"]) for r in self._conn.execute("SELECT proposal_id FROM proposals ORDER BY created_at")]

    # ---- approvals ------------------------------------------------------------------------------------------------------------
    def approvals_for(self, proposal_id: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM approvals WHERE proposal_id = ? ORDER BY approval_id", (proposal_id,))]

    def add_approval(self, proposal_id: str, role: str, approver_id: str, directory: dict[str, list[str]]) -> dict:
        """Record an approval, or raise StoreError. Refusals are written to the audit log BEFORE raising,
        because raising inside the transaction would roll the audit record back with everything else."""
        refusal, result = None, None
        with self._tx() as conn:
            p = self.get_proposal(proposal_id)
            if p is None:
                raise StoreError(f"Unknown proposal {proposal_id}.")
            case_id = p["content"]["case_id"]
            if p["status"] == "executed":
                refusal = "Proposal was already executed."
            elif role not in p["content"]["required_approvals"]:
                refusal = f"Role {role!r} is not required for this proposal."
            else:
                refusal = authority.validate_approval_request(role, approver_id, p["created_by"], directory)
            if refusal:
                self._audit(conn, f"user:{approver_id}", "approval_rejected", {"proposal_id": proposal_id, "role": role, "reason": refusal}, None, case_id)
            else:
                now = self._clock().isoformat()
                cur = conn.execute("INSERT INTO approvals (proposal_id, content_hash, role, approver_id, created_at) VALUES (?,?,?,?,?)",
                                   (proposal_id, p["content_hash"], role, approver_id, now))
                self._audit(conn, f"user:{approver_id}", "approval_granted",
                            {"proposal_id": proposal_id, "role": role, "content_hash": p["content_hash"], "approval_id": cur.lastrowid}, None, case_id)
                result = {"approval_id": cur.lastrowid, "proposal_id": proposal_id, "role": role, "approver_id": approver_id, "created_at": now}
        if refusal:
            raise StoreError(refusal)
        return result

    def revoke_approval(self, approval_id: int, revoked_by: str) -> dict:
        refusal, result = None, None
        with self._tx() as conn:
            r = conn.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
            if not r:
                raise StoreError(f"Unknown approval {approval_id}.")
            p = self.get_proposal(r["proposal_id"])
            case_id = p["content"]["case_id"] if p else None
            if r["revoked_at"]:
                refusal = "Approval was already revoked."
            elif p and p["status"] == "executed":
                refusal = "Cannot revoke an approval after execution."
            elif revoked_by != r["approver_id"]:
                refusal = "Only the approver can revoke their own approval."
            if refusal:
                self._audit(conn, f"user:{revoked_by}", "revocation_rejected", {"approval_id": approval_id, "reason": refusal}, None, case_id)
            else:
                now = self._clock().isoformat()
                conn.execute("UPDATE approvals SET revoked_at = ?, revoked_by = ? WHERE approval_id = ?", (now, revoked_by, approval_id))
                self._audit(conn, f"user:{revoked_by}", "approval_revoked",
                            {"proposal_id": r["proposal_id"], "approval_id": approval_id, "role": r["role"]}, None, case_id)
                result = {"approval_id": approval_id, "revoked_at": now}
        if refusal:
            raise StoreError(refusal)
        return result

    # ---- execution -------------------------------------------------------------------------------------------------------------
    def execute(self, proposal_id: str, actor: str, directory: dict[str, list[str]],
                action: Callable[[dict], dict]) -> dict:
        """Run the irreversible action at most once, only through the authority gate.

        `action(content) -> result` performs the side effect (payment, customs release). It runs inside the
        transaction, so a crash cannot leave 'executed' recorded without the action, or the reverse. If the action
        itself fails, nothing is recorded as executed, the failure is audited, and the caller may retry safely.
        """
        failure = None
        with self._tx() as conn:
            p = self.get_proposal(proposal_id)
            if p is None:
                raise StoreError(f"Unknown proposal {proposal_id}.")
            case_id = p["content"]["case_id"]
            prior = conn.execute("SELECT * FROM executions WHERE proposal_id = ?", (proposal_id,)).fetchone()
            if prior:
                self._audit(conn, actor, "execution_replay_ignored", {"proposal_id": proposal_id}, None, case_id)
                return {**json.loads(prior["result_json"]), "idempotent_replay": True}

            ok, reasons = authority.check_execution(p, self.approvals_for(proposal_id), False, directory, self._clock())
            if not ok:
                self._audit(conn, actor, "execution_blocked", {"proposal_id": proposal_id, "reasons": reasons}, None, case_id)
                return {"status": "blocked", "proposal_id": proposal_id, "reasons": reasons}

            try:
                result = {"status": "executed", "proposal_id": proposal_id, "case_id": case_id, **action(p["content"])}
            except Exception as exc:                      # the side effect failed: record nothing as done
                failure = f"{type(exc).__name__}: {exc}"
            else:
                conn.execute("INSERT INTO executions VALUES (?,?,?,?)", (proposal_id, self._clock().isoformat(), actor, json.dumps(result, default=str)))
                self._audit(conn, actor, "execution_completed",
                            {"proposal_id": proposal_id, "decision_type": p["content"]["decision_type"], "amount": p["content"]["amount"],
                             "confirmation_id": result.get("confirmation_id")}, None, case_id)
                return result
            self._audit(conn, actor, "execution_failed", {"proposal_id": proposal_id, "error": failure}, None, case_id)
        return {"status": "failed", "proposal_id": proposal_id, "error": failure}
