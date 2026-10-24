import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from glcc import cli
from glcc.demo import build_demo

DATA = Path(__file__).resolve().parent.parent / "data"


def run_cli(*argv, db=None):
    """Run the CLI in-process. Returns (exit_code, stdout)."""
    out = io.StringIO()
    code = 0
    full = (["--db", db] if db else []) + ["--data", str(DATA), *argv]
    with contextlib.redirect_stdout(out):
        try:
            cli.main(full)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            if isinstance(exc.code, str):
                out.write(exc.code)
    return code, out.getvalue()


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_corpus_lists_cases_and_warns_about_the_unreadable_scan(self):
        code, out = run_cli("corpus", "--clauses", db=self.db)
        self.assertEqual(code, 0)
        self.assertIn("CASE-1011", out)
        self.assertIn("VA-ZENITH-2024", out)
        self.assertIn("VA-ACME-2022-AMD2#1.1", out)
        self.assertIn("replaces VA-ACME-2022#5.1", out)

    def test_analyze_prints_decision_and_proposal(self):
        code, out = run_cli("analyze", "CASE-1001", db=self.db)
        self.assertEqual(code, 0)
        self.assertIn("decision=partial_settlement  amount=89000.0", out)
        self.assertIn("required approvals: underwriter", out)
        self.assertIn("clause_researcher[ok]", out)

    def test_analyze_memo_and_json_modes(self):
        _, memo = run_cli("analyze", "CASE-1001", "--memo", db=self.db)
        self.assertIn("# Resolution proposal for CASE-1001", memo)
        code, js = run_cli("analyze", "CASE-1002", "--json", db=self.db)
        data = json.loads(js)
        self.assertEqual((code, data["decision"], data["status"]), (0, "clear_customs_hold", "completed"))

    def test_unknown_case_exits_nonzero_without_a_traceback(self):
        code, out = run_cli("analyze", "CASE-9999", db=self.db)
        self.assertEqual(code, 2)
        self.assertIn("unknown case", out)
        self.assertNotIn("Traceback", out)

    def test_state_persists_across_invocations_and_the_gate_holds(self):
        _, out = run_cli("analyze", "CASE-1001", db=self.db)
        pid = [w for w in out.split() if w.startswith("PROP-")][0]
        code, out = run_cli("execute", pid, db=self.db)
        self.assertEqual(code, 1)
        self.assertIn("blocked", out)
        code, out = run_cli("approve", pid, "--role", "underwriter", "--as", "u-coord1", db=self.db)
        self.assertEqual(code, 1)
        self.assertIn("does not hold the role", out)
        code, out = run_cli("approve", pid, "--role", "underwriter", "--as", "u-uw1", db=self.db)
        self.assertEqual(code, 0)
        code, out = run_cli("execute", pid, db=self.db)
        self.assertEqual(code, 0)
        self.assertIn("executed", out)
        code, out = run_cli("execute", pid, db=self.db)                      # second time: a replay, still success, nothing repeated
        self.assertIn("idempotent_replay", out)
        _, listing = run_cli("proposals", db=self.db)
        self.assertIn("executed", listing)

    def test_audit_verify_passes_then_detects_tampering(self):
        run_cli("analyze", "CASE-1001", db=self.db)
        code, out = run_cli("audit", "--verify", db=self.db)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["valid"])
        import sqlite3
        con = sqlite3.connect(self.db)
        con.execute("UPDATE audit_log SET event = 'something_else' WHERE seq = 2")
        con.commit()
        con.close()
        code, out = run_cli("audit", "--verify", db=self.db)
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["valid"])

    def test_search_shows_ranks_and_exclusions(self):
        _, out = run_cli("search", "customs hold submission deadline", "--kind", "regulation", "--port", "Northgate", "--as-of", "2024-11-04", db=self.db)
        self.assertIn("PAN-REG-003.v1#2", out)
        self.assertIn("(excluded) PAN-REG-003.v2", out)

    def test_retrieval_eval_writes_a_report(self):
        out_file = Path(self.tmp.name) / "r.md"
        code, _ = run_cli("retrieval-eval", "--out", str(out_file), db=self.db)
        self.assertEqual(code, 0)
        self.assertIn("Recall@5", out_file.read_text())

    def test_simulated_llm_option_and_validation(self):
        code, out = run_cli("analyze", "CASE-1001", "--llm", "simulated:down", db=self.db)
        self.assertEqual(code, 0)
        self.assertIn("amount=89000.0", out)
        code, out = run_cli("analyze", "CASE-1001", "--llm", "simulated:wizard", db=self.db)
        self.assertNotEqual(code, 0)
        self.assertIn("--llm must be", out)

    def test_llm_check_explains_missing_configuration(self):
        with patch.dict(os.environ, {"GLCC_LLM_PROVIDER": "none"}):
            code, out = run_cli("llm-check", db=self.db)
        self.assertNotEqual(code, 0)
        self.assertIn("AZURE_OPENAI_ENDPOINT", out)
        with patch.dict(os.environ, {"GLCC_LLM_PROVIDER": "azure_openai"}, clear=False):
            os.environ.pop("AZURE_OPENAI_ENDPOINT", None)
            code, out = run_cli("llm-check", db=self.db)
        self.assertNotEqual(code, 0)
        self.assertIn("requires", out)

    def test_missing_data_directory_is_a_clean_error(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cli.main(["--data", "/no/such/dir", "corpus"])
        self.assertIn("data directory not found", str(ctx.exception.code))


class TestDemo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        cls.result = build_demo(cls.out, DATA)
        cls.report = (cls.out / "DEMO_REPORT.md").read_text()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_all_artifacts_are_written(self):
        for name in ("DEMO_REPORT.md", "audit_export.jsonl", "retrieval_eval.md"):
            self.assertTrue((self.out / name).exists(), name)
        self.assertEqual(len(list((self.out / "memos").glob("CASE-*.md"))), 11)

    def test_every_scenario_completed_with_the_expected_decision(self):
        decisions = {cid: r["decision"] for cid, r in self.result["results"].items()}
        self.assertEqual(decisions["CASE-1001"], "partial_settlement")
        self.assertEqual(decisions["CASE-1005"], "escalate_to_legal")
        self.assertEqual(decisions["CASE-1011"], "request_information")
        self.assertTrue(all(r["status"] == "completed" for r in self.result["results"].values()))

    def test_governance_walkthrough_outcomes(self):
        steps = {(c, a): o for c, a, o, _ in self.result["governance_steps"]}
        self.assertEqual(steps[("CASE-1001", "execute with no approvals")], "BLOCKED")
        self.assertEqual(steps[("CASE-1001", "execute")], "EXECUTED")
        self.assertEqual(steps[("CASE-1001", "execute again (retry/double-click)")], "NO-OP")
        self.assertEqual(steps[("CASE-1006", "execute with one of two required approvals")], "BLOCKED")
        self.assertEqual(steps[("CASE-1005", "legal counsel approves an escalation, then execute")], "BLOCKED")
        self.assertEqual(steps[("CASE-1008", "TAMPER: amount edited in the database after approval")], "BLOCKED")
        self.assertEqual(steps[("(audit log)", "TAMPER: an approval record edited in a copy")], "DETECTED")
        refused = [o for _, a, o, _ in self.result["governance_steps"] if "approves as" in a]
        self.assertEqual(refused.count("REFUSED"), 4)

    def test_the_model_never_changes_the_outcome_in_any_simulated_mode(self):
        self.assertEqual(len(self.result["llm_rows"]), 6)
        self.assertTrue(all(row[3] == "same" for row in self.result["llm_rows"]))

    def test_audit_chain_is_valid_and_exported_records_verify(self):
        self.assertTrue(self.result["chain"]["valid"])
        from glcc.governance.audit import verify_chain
        records = [json.loads(line) for line in (self.out / "audit_export.jsonl").read_text().splitlines()]
        self.assertTrue(verify_chain(records)["valid"])
        self.assertEqual(len(records), self.result["chain"]["records"])

    def test_exported_audit_log_can_be_independently_re_verified_and_tampering_is_caught(self):
        from glcc.governance.audit import verify_chain
        records = [json.loads(line) for line in (self.out / "audit_export.jsonl").read_text().splitlines()]
        records[10]["payload"]["tampered"] = True
        self.assertFalse(verify_chain(records)["valid"])

    def test_report_contains_every_section_and_real_numbers(self):
        for heading in ("## 1. Scenarios", "## 2. How one case runs", "## 3. Governance walkthrough", "## 4. What a language model can and cannot change",
                        "## 5. Audit trail", "## 6. Retrieval quality"):
            self.assertIn(heading, self.report)
        self.assertIn("89,000.00", self.report)
        self.assertIn("142,500.00", self.report)
        self.assertIn("valid = True", self.report)
        self.assertIn("VA-ZENITH-2024", self.report)

    def test_memos_state_that_nothing_was_executed(self):
        for p in (self.out / "memos").glob("CASE-*.md"):
            self.assertIn("No action has been taken", p.read_text(), p.name)


if __name__ == "__main__":
    unittest.main()
