"""Tamper-evident audit records.

Each record stores the hash of the previous record and its own sequence number, then hashes itself.
Editing a record changes its hash; deleting or reordering records breaks the previous-hash link and
the sequence; so any alteration is detectable by recomputing the chain. This is evidence of
tampering, not prevention: in production also ship the log to write-once storage (see docs).
"""

from __future__ import annotations

import hashlib
import json

GENESIS = "0" * 64
HASHED_FIELDS = ("seq", "ts", "actor", "event", "run_id", "case_id", "payload", "prev_hash")


def _digest(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def make_record(prev_hash: str, seq: int, ts: str, actor: str, event: str, payload: dict,
                run_id: str | None = None, case_id: str | None = None) -> dict:
    body = {"seq": seq, "ts": ts, "actor": actor, "event": event, "run_id": run_id, "case_id": case_id,
            "payload": payload, "prev_hash": prev_hash}
    return {**body, "hash": _digest(body)}


def verify_chain(records: list[dict]) -> dict:
    """Recompute the whole chain. Returns {"valid", "records", "first_bad_seq", "reason"}."""
    prev, expected_seq = GENESIS, 1
    for rec in records:
        body = {k: rec.get(k) for k in HASHED_FIELDS}
        if rec.get("seq") != expected_seq:
            return {"valid": False, "records": len(records), "first_bad_seq": rec.get("seq"),
                    "reason": f"sequence gap or reorder: expected {expected_seq}, found {rec.get('seq')}"}
        if body["prev_hash"] != prev:
            return {"valid": False, "records": len(records), "first_bad_seq": rec.get("seq"), "reason": "previous-hash link broken"}
        if rec.get("hash") != _digest(body):
            return {"valid": False, "records": len(records), "first_bad_seq": rec.get("seq"), "reason": "record content does not match its hash"}
        prev, expected_seq = rec["hash"], expected_seq + 1
    return {"valid": True, "records": len(records), "first_bad_seq": None, "reason": None}
