import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from glcc.governance import audit, authority as auth
from glcc.governance.store import Store, StoreError

DIRECTORY = {"u-uw": ["underwriter"], "u-uw2": ["underwriter"], "u-sr": ["senior_underwriter"], "u-legal": ["legal_counsel"],
             "u-comp": ["compliance_officer"], "u-coord": ["logistics_coordinator"]}


class Clock:
    def __init__(self):
        self.now = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def proposal(decision="partial_settlement", amount=89000.0, case="CASE-1001", flags=(), created_by="glcc-agent"):
    content = {"case_id": case, "decision_type": decision, "amount": amount, "currency": "USD",
               "required_approvals": auth.required_approvals(decision, amount, list(flags)), "flags": list(flags)}
    digest = auth.content_hash(content)
    return {"proposal_id": auth.proposal_id_for(digest), "content_hash": digest, "content": content, "created_by": created_by}


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.calls = []

    def tearDown(self):
        self.store.close()

    def action(self, content):
        self.calls.append(content["case_id"])
        return {"confirmation_id": f"CONF-{len(self.calls)}"}

    def events(self):
        return [r["event"] for r in self.store.audit_records()]

    def create(self, **kw):
        p, _ = self.store.save_proposal(proposal(**kw), "run-1", "agent:glcc")
        return p


class TestAuditChain(StoreTestCase):
    def test_empty_chain_is_valid(self):
        self.assertTrue(self.store.verify_audit()["valid"])

    def test_appended_records_chain(self):
        for i in range(5):
            self.store.append_audit("agent:glcc", "step", {"i": i})
        v = self.store.verify_audit()
        self.assertEqual((v["valid"], v["records"]), (True, 5))
        recs = self.store.audit_records()
        self.assertEqual(recs[0]["prev_hash"], audit.GENESIS)
        self.assertEqual(recs[1]["prev_hash"], recs[0]["hash"])

    def test_editing_a_record_is_detected(self):
        for i in range(4):
            self.store.append_audit("a", "e", {"i": i})
        self.store._conn.execute("UPDATE audit_log SET payload_json = '{\"i\":99}' WHERE seq = 2")
        v = self.store.verify_audit()
        self.assertFalse(v["valid"])
        self.assertEqual(v["first_bad_seq"], 2)

    def test_deleting_a_record_is_detected(self):
        for i in range(4):
            self.store.append_audit("a", "e", {"i": i})
        self.store._conn.execute("DELETE FROM audit_log WHERE seq = 2")
        self.assertFalse(self.store.verify_audit()["valid"])

    def test_deleting_the_last_record_is_not_detectable_by_the_chain_alone(self):
        # Honest limit: truncation of the tail is undetectable without an external anchor. This test documents it.
        for i in range(3):
            self.store.append_audit("a", "e", {"i": i})
        self.store._conn.execute("DELETE FROM audit_log WHERE seq = 3")
        self.assertTrue(self.store.verify_audit()["valid"])

    def test_reordering_is_detected(self):
        for i in range(4):
            self.store.append_audit("a", "e", {"i": i})
        self.store._conn.execute("UPDATE audit_log SET seq = 99 WHERE seq = 2")
        self.store._conn.execute("UPDATE audit_log SET seq = 2 WHERE seq = 3")
        self.store._conn.execute("UPDATE audit_log SET seq = 3 WHERE seq = 99")
        self.assertFalse(self.store.verify_audit()["valid"])

    def test_pii_in_payloads_is_redacted_before_it_is_stored(self):
        self.store.append_audit("a", "e", {"note": "contact a.b@example.com or 555-123-4567"})
        text = str(self.store.audit_records()[0]["payload"])
        self.assertNotIn("a.b@example.com", text)
        self.assertNotIn("555-123-4567", text)

    def test_filtering_by_case(self):
        self.store.append_audit("a", "e1", {}, case_id="CASE-1")
        self.store.append_audit("a", "e2", {}, case_id="CASE-2")
        self.assertEqual([r["event"] for r in self.store.audit_records(case_id="CASE-2")], ["e2"])

    def test_concurrent_appends_keep_the_chain_valid(self):
        def worker(n):
            for i in range(20):
                self.store.append_audit(f"t{n}", "e", {"i": i})
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        v = self.store.verify_audit()
        self.assertEqual((v["valid"], v["records"]), (True, 120))

    def test_chain_survives_reopening_a_file_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "glcc.db"
            s1 = Store(path, clock=self.clock)
            s1.append_audit("a", "e", {"i": 1})
            s1.append_audit("a", "e", {"i": 2})
            s1.close()
            s2 = Store(path, clock=self.clock)
            s2.append_audit("a", "e", {"i": 3})
            v = s2.verify_audit()
            s2.close()
        self.assertEqual((v["valid"], v["records"]), (True, 3))


class TestProposals(StoreTestCase):
    def test_save_is_idempotent_and_audited(self):
        p1, created1 = self.store.save_proposal(proposal(), "run-1", "agent:glcc")
        p2, created2 = self.store.save_proposal(proposal(), "run-2", "agent:glcc")
        self.assertEqual((created1, created2), (True, False))
        self.assertEqual(p1["proposal_id"], p2["proposal_id"])
        self.assertEqual(len(self.store.list_proposals()), 1)
        self.assertEqual(self.events(), ["proposal_created", "proposal_reused"])

    def test_different_content_gets_a_different_id(self):
        self.assertNotEqual(proposal(amount=1000.0)["proposal_id"], proposal(amount=2000.0)["proposal_id"])

    def test_status_starts_pending(self):
        self.assertEqual(self.create()["status"], "pending_approval")

    def test_run_round_trip(self):
        run = {"run_id": "r1", "case_id": "CASE-1001", "started_at": "t0", "finished_at": "t1", "mode": "offline", "status": "completed",
               "decision": "partial_settlement", "proposal_id": "PROP-x", "trace": [{"agent": "intake"}], "analysis": {"a": 1}, "memo": "m"}
        self.store.save_run(run)
        got = self.store.get_run("r1")
        self.assertEqual((got["trace"], got["analysis"], got["memo"]), ([{"agent": "intake"}], {"a": 1}, "m"))
        self.assertIsNone(self.store.get_run("nope"))


class TestApprovals(StoreTestCase):
    def test_grant_is_bound_to_the_content_hash_and_audited(self):
        p = self.create()
        a = self.store.add_approval(p["proposal_id"], "underwriter", "u-uw", DIRECTORY)
        stored = self.store.approvals_for(p["proposal_id"])[0]
        self.assertEqual(stored["content_hash"], p["content_hash"])
        self.assertEqual(a["role"], "underwriter")
        self.assertIn("approval_granted", self.events())

    def test_every_refusal_is_audited(self):
        """Regression: refusals used to be rolled back with the transaction, erasing the record of the attempt."""
        p = self.create()
        pid = p["proposal_id"]
        attempts = [("underwriter", "u-coord", "does not hold"), ("underwriter", "system", "cannot approve its own"),
                    ("underwriter", "glcc-agent", "cannot approve its own"), ("compliance_officer", "u-comp", "not required")]
        for role, who, expect in attempts:
            with self.assertRaises(StoreError) as ctx:
                self.store.add_approval(pid, role, who, DIRECTORY)
            self.assertIn(expect, str(ctx.exception))
        rejected = [r for r in self.store.audit_records() if r["event"] == "approval_rejected"]
        self.assertEqual(len(rejected), len(attempts))
        self.assertEqual(self.store.approvals_for(pid), [])
        self.assertTrue(self.store.verify_audit()["valid"])

    def test_creator_cannot_approve(self):
        p = self.create(created_by="u-uw")
        with self.assertRaises(StoreError):
            self.store.add_approval(p["proposal_id"], "underwriter", "u-uw", DIRECTORY)

    def test_unknown_proposal(self):
        with self.assertRaises(StoreError):
            self.store.add_approval("PROP-nope", "underwriter", "u-uw", DIRECTORY)

    def test_revoke_only_by_the_approver_and_audited(self):
        p = self.create()
        a = self.store.add_approval(p["proposal_id"], "underwriter", "u-uw", DIRECTORY)
        with self.assertRaises(StoreError) as ctx:
            self.store.revoke_approval(a["approval_id"], "u-uw2")
        self.assertIn("Only the approver", str(ctx.exception))
        self.assertIn("revocation_rejected", self.events())
        self.store.revoke_approval(a["approval_id"], "u-uw")
        self.assertIn("approval_revoked", self.events())
        with self.assertRaises(StoreError):
            self.store.revoke_approval(a["approval_id"], "u-uw")        # already revoked

    def test_cannot_approve_or_revoke_after_execution(self):
        p = self.create()
        a = self.store.add_approval(p["proposal_id"], "underwriter", "u-uw", DIRECTORY)
        self.store.execute(p["proposal_id"], "user:ops", DIRECTORY, self.action)
        with self.assertRaises(StoreError):
            self.store.revoke_approval(a["approval_id"], "u-uw")
        with self.assertRaises(StoreError):
            self.store.add_approval(p["proposal_id"], "underwriter", "u-uw2", DIRECTORY)


class TestExecution(StoreTestCase):
    def test_blocked_until_approved_and_the_attempt_is_audited(self):
        p = self.create()
        r = self.store.execute(p["proposal_id"], "user:ops", DIRECTORY, self.action)
        self.assertEqual(r["status"], "blocked")
        self.assertEqual(self.calls, [])
        self.assertIn("execution_blocked", self.events())
        self.assertEqual(self.store.get_proposal(p["proposal_id"])["status"], "pending_approval")

    def test_executes_once_when_approved(self):
        p = self.create()
        self.store.add_approval(p["proposal_id"], "underwriter", "u-uw", DIRECTORY)
        r = self.store.execute(p["proposal_id"], "user:ops", DIRECTORY, self.action)
        self.assertEqual((r["status"], r["confirmation_id"]), ("executed", "CONF-1"))
        self.assertEqual(self.store.get_proposal(p["proposal_id"])["status"], "executed")
        self.assertIn("execution_completed", self.events())

    def test_replay_does_not_repeat_the_action(self):
        p = self.create()
        self.store.add_approval(p["proposal_id"], "underwriter", "u-uw", DIRECTORY)
        first = self.store.execute(p["proposal_id"], "user:ops", DIRECTORY, self.action)
        again = self.store.execute(p["proposal_id"], "user:ops", DIRECTORY, self.action)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(again["idempotent_replay"])
        self.assertEqual(again["confirmation_id"], first["confirmation_id"])
        self.assertIn("execution_replay_ignored", self.events())

    def test_concurrent_execution_pays_exactly_once(self):
        p = self.create()
        self.store.add_approval(p["proposal_id"], "underwriter", "u-uw", DIRECTORY)
        results = []

        def go():
            results.append(self.store.execute(p["proposal_id"], "user:ops", DIRECTORY, self.action))
        threads = [threading.Thread(target=go) for _ in range(10)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum(1 for r in results if r["status"] == "executed" and not r.get("idempotent_replay")), 1)
        self.assertEqual(len({r["confirmation_id"] for r in results}), 1)
        self.assertTrue(self.store.verify_audit()["valid"])

    def test_failed_action_records_nothing_as_executed_and_can_be_retried(self):
        p = self.create()
        self.store.add_approval(p["proposal_id"], "underwriter", "u-uw", DIRECTORY)

        def boom(content):
            raise RuntimeError("payment gateway down")
        r = self.store.execute(p["proposal_id"], "user:ops", DIRECTORY, boom)
        self.assertEqual(r["status"], "failed")
        self.assertIn("payment gateway down", r["error"])
        self.assertEqual(self.store.get_proposal(p["proposal_id"])["status"], "pending_approval")
        self.assertIn("execution_failed", self.events())
        retry = self.store.execute(p["proposal_id"], "user:ops", DIRECTORY, self.action)
        self.assertEqual(retry["status"], "executed")
        self.assertTrue(self.store.verify_audit()["valid"])

    def test_approval_expires_and_blocks_execution(self):
        p = self.create()
        self.store.add_approval(p["proposal_id"], "underwriter", "u-uw", DIRECTORY)
        self.clock.advance(hours=auth.APPROVAL_TTL_HOURS + 1)
        r = self.store.execute(p["proposal_id"], "user:ops", DIRECTORY, self.action)
        self.assertEqual(r["status"], "blocked")
        self.assertIn("expired", r["reasons"][0])
        self.assertEqual(self.calls, [])

    def test_revoked_approval_blocks_execution(self):
        p = self.create()
        a = self.store.add_approval(p["proposal_id"], "underwriter", "u-uw", DIRECTORY)
        self.store.revoke_approval(a["approval_id"], "u-uw")
        self.assertEqual(self.store.execute(p["proposal_id"], "user:ops", DIRECTORY, self.action)["status"], "blocked")

    def test_role_revoked_in_the_directory_blocks_execution(self):
        p = self.create()
        self.store.add_approval(p["proposal_id"], "underwriter", "u-uw", DIRECTORY)
        r = self.store.execute(p["proposal_id"], "user:ops", {**DIRECTORY, "u-uw": []}, self.action)
        self.assertEqual(r["status"], "blocked")
        self.assertIn("re-checked at execution", r["reasons"][0])

    def test_dual_control_end_to_end(self):
        p = self.create(decision="approve_settlement", amount=150000.0)
        pid = p["proposal_id"]
        self.assertEqual(p["content"]["required_approvals"], ["senior_underwriter", "legal_counsel"])
        self.store.add_approval(pid, "senior_underwriter", "u-sr", DIRECTORY)
        self.assertEqual(self.store.execute(pid, "user:ops", DIRECTORY, self.action)["status"], "blocked")      # one of two
        self.store.add_approval(pid, "legal_counsel", "u-legal", DIRECTORY)
        self.assertEqual(self.store.execute(pid, "user:ops", DIRECTORY, self.action)["status"], "executed")

    def test_non_executable_proposal_never_executes(self):
        p = self.create(decision="escalate_to_legal", amount=0.0)
        self.store.add_approval(p["proposal_id"], "legal_counsel", "u-legal", DIRECTORY)
        r = self.store.execute(p["proposal_id"], "user:ops", DIRECTORY, self.action)
        self.assertEqual(r["status"], "blocked")
        self.assertIn("not executable", r["reasons"][0])

    def test_unknown_proposal(self):
        with self.assertRaises(StoreError):
            self.store.execute("PROP-nope", "user:ops", DIRECTORY, self.action)

    def test_tampering_with_stored_content_blocks_execution(self):
        p = self.create()
        self.store.add_approval(p["proposal_id"], "underwriter", "u-uw", DIRECTORY)
        self.store._conn.execute("UPDATE proposals SET content_json = replace(content_json, '89000.0', '999999.0') WHERE proposal_id = ?",
                                 (p["proposal_id"],))
        r = self.store.execute(p["proposal_id"], "user:ops", DIRECTORY, self.action)
        self.assertEqual(r["status"], "blocked")
        self.assertIn("hash", r["reasons"][0])
        self.assertEqual(self.calls, [])

    def test_full_lifecycle_audit_trail_is_ordered_and_valid(self):
        p = self.create()
        self.store.execute(p["proposal_id"], "user:ops", DIRECTORY, self.action)
        self.store.add_approval(p["proposal_id"], "underwriter", "u-uw", DIRECTORY)
        self.store.execute(p["proposal_id"], "user:ops", DIRECTORY, self.action)
        self.assertEqual(self.events(), ["proposal_created", "execution_blocked", "approval_granted", "execution_completed"])
        self.assertTrue(self.store.verify_audit()["valid"])


if __name__ == "__main__":
    unittest.main()
