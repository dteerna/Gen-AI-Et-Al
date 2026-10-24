import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from glcc.api.server import IDENTITY_HEADER, MAX_BODY, make_server
from glcc.config import load_directory
from glcc.corpus import Corpus
from glcc.governance.store import Store
from glcc.service import ExceptionService

DATA = Path(__file__).resolve().parent.parent / "data"
CORPUS = Corpus.load(DATA)
KEY = "test-key-123"


class Client:
    def __init__(self, base, key=KEY):
        self.base, self.key = base, key

    def call(self, method, path, body=None, headers=None, raw=None, key="default"):
        h = {"Content-Type": "application/json", **(headers or {})}
        k = self.key if key == "default" else key
        if k:
            h["X-API-Key"] = k
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    def json(self, *a, **k):
        status, text, headers = self.call(*a, **k)
        return status, json.loads(text), headers


class ApiTestCase(unittest.TestCase):
    allow_body = True

    def setUp(self):
        self.service = ExceptionService(CORPUS, Store(":memory:"), None, load_directory())
        self.server = make_server(self.service, "127.0.0.1", 0, api_key=KEY, allow_body_identity=self.allow_body)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.api = Client(f"http://127.0.0.1:{self.server.server_address[1]}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.service.store.close()

    def analyze(self, case="CASE-1001"):
        status, body, _ = self.api.json("POST", f"/cases/{case}/analyze")
        self.assertEqual(status, 200, body)
        return body


class TestAuthentication(ApiTestCase):
    def test_health_is_open_and_everything_else_needs_the_key(self):
        self.assertEqual(self.api.json("GET", "/health", key=None)[0], 200)
        for path in ("/cases", "/proposals", "/audit", "/audit/verify"):
            self.assertEqual(self.api.json("GET", path, key=None)[0], 401, path)
            self.assertEqual(self.api.json("GET", path, key="wrong")[0], 401, path)
            self.assertEqual(self.api.json("GET", path)[0], 200, path)

    def test_post_endpoints_are_protected_too(self):
        self.assertEqual(self.api.json("POST", "/cases/CASE-1001/analyze", key=None)[0], 401)
        self.assertEqual(self.api.json("POST", "/proposals/PROP-0123456789/execute", {}, key="nope")[0], 401)

    def test_health_reports_mode_corpus_and_audit_state(self):
        _, body, _ = self.api.json("GET", "/health", key=None)
        self.assertEqual((body["status"], body["mode"], body["audit"]), ("ok", "offline", True))
        self.assertEqual(body["corpus"]["clauses"], 31)

    def test_responses_carry_hardening_headers_and_never_cache(self):
        _, _, headers = self.api.json("GET", "/cases")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")


class TestAnalysisEndpoints(ApiTestCase):
    def test_cases_and_analysis(self):
        self.assertEqual(len(self.api.json("GET", "/cases")[1]["cases"]), 11)
        body = self.analyze("CASE-1001")
        self.assertEqual((body["decision"], body["amount"], body["required_approvals"]), ("partial_settlement", 89000.0, ["underwriter"]))
        self.assertEqual(body["agents"][0]["agent"], "intake")
        self.assertTrue(body["proposal_id"].startswith("PROP-"))

    def test_unknown_and_malformed_cases(self):
        self.assertEqual(self.api.json("POST", "/cases/CASE-9999/analyze")[0], 404)
        self.assertEqual(self.api.json("POST", "/cases/..%2Fdata/analyze")[0], 404)
        self.assertEqual(self.api.json("POST", "/cases/CASE%201001/analyze")[0], 404)

    def test_run_proposal_and_memo_retrieval(self):
        body = self.analyze("CASE-1003")
        status, run, _ = self.api.json("GET", f"/runs/{body['run_id']}")
        self.assertEqual((status, run["decision"]), (200, "approve_settlement"))
        status, view, _ = self.api.json("GET", f"/proposals/{body['proposal_id']}")
        self.assertEqual((status, view["status"], view["ready_to_execute"]), (200, "pending_approval", False))
        status, text, headers = self.api.call("GET", f"/proposals/{body['proposal_id']}/memo")
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", headers["Content-Type"])
        self.assertIn("No action has been taken", text)
        self.assertEqual(self.api.json("GET", "/proposals/PROP-0000000000")[0], 404)
        self.assertEqual(self.api.json("GET", "/runs/RUN-nope")[0], 404)

    def test_proposal_listing(self):
        self.analyze("CASE-1001")
        self.analyze("CASE-1005")
        items = self.api.json("GET", "/proposals")[1]["proposals"]
        self.assertEqual({i["case_id"] for i in items}, {"CASE-1001", "CASE-1005"})

    def test_search_endpoint(self):
        status, body, _ = self.api.json("GET", "/search?kind=clause&q=liability+cap&vendor=ACME+Freight")
        self.assertEqual(status, 200)
        self.assertIn("VA-ACME-2022-AMD2#1.1", [r["clause_id"] for r in body["results"]])
        _, reg, _ = self.api.json("GET", "/search?kind=regulation&q=customs+hold+deadline&port=Northgate&as_of=2024-11-04")
        ids = [r["section_id"] for r in reg["results"] if "section_id" in r]
        self.assertIn("PAN-REG-003.v1#2", ids)
        self.assertEqual(self.api.json("GET", "/search")[0], 400)
        self.assertEqual(self.api.json("GET", "/search?kind=bogus&q=x")[0], 400)


class TestApprovalFlow(ApiTestCase):
    def test_full_lifecycle_over_http(self):
        pid = self.analyze("CASE-1001")["proposal_id"]
        status, body, _ = self.api.json("POST", f"/proposals/{pid}/execute", {"approver_id": "u-ops"})
        self.assertEqual((status, body["status"]), (409, "blocked"))
        status, body, _ = self.api.json("POST", f"/proposals/{pid}/approvals", {"role": "underwriter", "approver_id": "u-uw1"})
        self.assertEqual((status, body["role"]), (201, "underwriter"))
        self.assertTrue(self.api.json("GET", f"/proposals/{pid}")[1]["ready_to_execute"])
        status, body, _ = self.api.json("POST", f"/proposals/{pid}/execute", {"approver_id": "u-ops"})
        self.assertEqual((status, body["status"]), (200, "executed"))
        status, again, _ = self.api.json("POST", f"/proposals/{pid}/execute", {"approver_id": "u-ops"})
        self.assertEqual((status, again["idempotent_replay"], again["confirmation_id"]), (200, True, body["confirmation_id"]))
        self.assertEqual(self.api.json("GET", f"/proposals/{pid}")[1]["status"], "executed")

    def test_refused_approvals_are_409_with_the_reason(self):
        pid = self.analyze("CASE-1001")["proposal_id"]
        cases = [({"role": "underwriter", "approver_id": "u-coord1"}, "does not hold"),
                 ({"role": "underwriter", "approver_id": "system"}, "cannot approve its own"),
                 ({"role": "legal_counsel", "approver_id": "u-legal1"}, "not required"),
                 ({"role": "underwriter", "approver_id": "ghost"}, "does not hold")]
        for body, expect in cases:
            status, out, _ = self.api.json("POST", f"/proposals/{pid}/approvals", body)
            self.assertEqual(status, 409, body)
            self.assertIn(expect, out["error"])
        self.assertEqual(self.api.json("POST", f"/proposals/{pid}/approvals", {"approver_id": "u-uw1"})[0], 400)       # no role
        refusals = [r for r in self.api.json("GET", "/audit?limit=100")[1]["records"] if r["event"] == "approval_rejected"]
        self.assertEqual(len(refusals), 4)                                                                             # every refusal audited

    def test_dual_control_over_http(self):
        pid = self.analyze("CASE-1006")["proposal_id"]
        self.api.json("POST", f"/proposals/{pid}/approvals", {"role": "senior_underwriter", "approver_id": "u-sr1"})
        status, body, _ = self.api.json("POST", f"/proposals/{pid}/execute", {"approver_id": "u-ops"})
        self.assertEqual(status, 409)
        self.assertIn("legal_counsel", body["reasons"][0])
        self.api.json("POST", f"/proposals/{pid}/approvals", {"role": "legal_counsel", "approver_id": "u-legal1"})
        self.assertEqual(self.api.json("POST", f"/proposals/{pid}/execute", {"approver_id": "u-ops"})[0], 200)

    def test_revocation_over_http(self):
        pid = self.analyze("CASE-1001")["proposal_id"]
        aid = self.api.json("POST", f"/proposals/{pid}/approvals", {"role": "underwriter", "approver_id": "u-uw1"})[1]["approval_id"]
        self.assertEqual(self.api.json("POST", f"/approvals/{aid}/revoke", {"approver_id": "u-uw2"})[0], 409)       # not their approval
        self.assertEqual(self.api.json("POST", f"/approvals/{aid}/revoke", {"approver_id": "u-uw1"})[0], 200)
        self.assertEqual(self.api.json("POST", f"/proposals/{pid}/execute", {"approver_id": "u-ops"})[0], 409)

    def test_ten_simultaneous_execute_requests_pay_once(self):
        pid = self.analyze("CASE-1001")["proposal_id"]
        self.api.json("POST", f"/proposals/{pid}/approvals", {"role": "underwriter", "approver_id": "u-uw1"})
        results = []

        def go():
            results.append(self.api.json("POST", f"/proposals/{pid}/execute", {"approver_id": "u-ops"}))
        threads = [threading.Thread(target=go) for _ in range(10)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual({s for s, _, _ in results}, {200})
        self.assertEqual(sum(1 for _, b, _ in results if not b.get("idempotent_replay")), 1)
        self.assertEqual(len({b["confirmation_id"] for _, b, _ in results}), 1)
        events = [r["event"] for r in self.api.json("GET", "/audit?limit=1000")[1]["records"]]
        self.assertEqual(events.count("execution_completed"), 1)

    def test_a_non_executable_proposal_cannot_be_executed_even_if_approved(self):
        pid = self.analyze("CASE-1005")["proposal_id"]
        self.api.json("POST", f"/proposals/{pid}/approvals", {"role": "legal_counsel", "approver_id": "u-legal1"})
        status, body, _ = self.api.json("POST", f"/proposals/{pid}/execute", {"approver_id": "u-ops"})
        self.assertEqual(status, 409)
        self.assertIn("not executable", body["reasons"][0])


class TestIdentityHandling(ApiTestCase):
    allow_body = False

    def test_body_identity_is_ignored_when_not_allowed(self):
        pid = self.analyze("CASE-1001")["proposal_id"]
        status, body, _ = self.api.json("POST", f"/proposals/{pid}/approvals", {"role": "underwriter", "approver_id": "u-uw1"})
        self.assertEqual(status, 401)
        self.assertIn(IDENTITY_HEADER, body["error"])

    def test_the_authenticated_header_wins_over_anything_in_the_body(self):
        pid = self.analyze("CASE-1001")["proposal_id"]
        status, body, _ = self.api.json("POST", f"/proposals/{pid}/approvals", {"role": "underwriter", "approver_id": "u-legal1"},
                                        headers={IDENTITY_HEADER: "u-uw1"})
        self.assertEqual((status, body["approver_id"]), (201, "u-uw1"))

    def test_execute_requires_an_identity_too(self):
        pid = self.analyze("CASE-1001")["proposal_id"]
        self.assertEqual(self.api.json("POST", f"/proposals/{pid}/execute", {})[0], 401)
        self.assertEqual(self.api.json("POST", f"/proposals/{pid}/execute", {}, headers={IDENTITY_HEADER: "u-ops"})[0], 409)

    def test_a_spoofed_role_is_still_checked_against_the_directory(self):
        pid = self.analyze("CASE-1001")["proposal_id"]
        status, body, _ = self.api.json("POST", f"/proposals/{pid}/approvals", {"role": "underwriter"}, headers={IDENTITY_HEADER: "u-viewer"})
        self.assertEqual(status, 409)
        self.assertIn("does not hold", body["error"])


class TestRobustness(ApiTestCase):
    def test_oversized_body_is_rejected(self):
        pid = self.analyze("CASE-1001")["proposal_id"]
        status, body, _ = self.api.json("POST", f"/proposals/{pid}/approvals", raw=b"{" + b" " * (MAX_BODY + 10) + b"}")
        self.assertEqual(status, 413)

    def test_invalid_and_non_object_json(self):
        pid = self.analyze("CASE-1001")["proposal_id"]
        self.assertEqual(self.api.json("POST", f"/proposals/{pid}/approvals", raw=b"{not json")[0], 400)
        self.assertEqual(self.api.json("POST", f"/proposals/{pid}/approvals", raw=b"[1,2,3]")[0], 400)

    def test_unknown_routes_and_methods(self):
        for method, path in (("GET", "/nope"), ("POST", "/health"), ("GET", "/proposals/not-an-id"), ("POST", "/proposals/PROP-0123456789"),
                             ("GET", "/proposals/PROP-0123456789/execute")):
            self.assertEqual(self.api.json(method, path)[0], 404, (method, path))

    def test_internal_errors_do_not_leak_details(self):
        with self.assertLogs("glcc.api", level="ERROR") as logs, \
                patch.object(ExceptionService, "analyze", side_effect=RuntimeError("secret database password is hunter2")):
            status, text, _ = self.api.call("POST", "/cases/CASE-1001/analyze")
        self.assertIn("hunter2", "\n".join(logs.output))          # operators get the detail in the server log...
        self.assertEqual(status, 500)
        self.assertNotIn("hunter2", text)                          # ...and the client gets nothing
        self.assertEqual(json.loads(text), {"error": "internal error"})

    def test_audit_endpoints_detect_tampering(self):
        self.analyze("CASE-1001")
        self.assertTrue(self.api.json("GET", "/audit/verify")[1]["valid"])
        self.service.store._conn.execute("UPDATE audit_log SET actor = 'someone-else' WHERE seq = 3")
        v = self.api.json("GET", "/audit/verify")[1]
        self.assertEqual((v["valid"], v["first_bad_seq"]), (False, 3))
        self.assertFalse(self.api.json("GET", "/health", key=None)[1]["audit"])

    def test_audit_limit_is_clamped(self):
        self.analyze("CASE-1001")
        self.assertLessEqual(len(self.api.json("GET", "/audit?limit=5")[1]["records"]), 5)
        self.assertGreaterEqual(len(self.api.json("GET", "/audit?limit=-4")[1]["records"]), 1)


class TestServiceLayer(unittest.TestCase):
    def setUp(self):
        self.svc = ExceptionService(CORPUS, Store(":memory:"), None, load_directory())

    def test_proposal_view_explains_why_it_cannot_execute_yet(self):
        pid = self.svc.analyze("CASE-1001")["proposal"]["proposal_id"]
        v = self.svc.proposal_view(pid)
        self.assertFalse(v["ready_to_execute"])
        self.assertIn("underwriter", v["blocked_reasons"][0])
        self.svc.approve(pid, "underwriter", "u-uw1")
        self.assertTrue(self.svc.proposal_view(pid)["ready_to_execute"])
        self.svc.execute(pid, "user:ops")
        self.assertEqual(self.svc.proposal_view(pid)["blocked_reasons"], [])

    def test_unknown_proposal_view_and_memo(self):
        self.assertIsNone(self.svc.proposal_view("PROP-0000000000"))
        self.assertIsNone(self.svc.memo("PROP-0000000000"))

    def test_summarize_run(self):
        s = self.svc.summarize_run(self.svc.analyze("CASE-1004"))
        self.assertEqual((s["decision"], s["flags"], s["required_approvals"]), ("approve_settlement", ["injection_suspected"], ["underwriter", "legal_counsel"]))

    def test_search_validates_kind(self):
        with self.assertRaises(ValueError):
            self.svc.search("bogus", "x")
        self.assertTrue(self.svc.search("ruling", "force majeure late written notice"))


if __name__ == "__main__":
    unittest.main()
