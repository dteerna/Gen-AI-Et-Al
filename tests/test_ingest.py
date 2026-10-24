import json
import shutil
import tempfile
import unittest
from pathlib import Path

from glcc.corpus import Corpus
from glcc.ingest.common import IngestError, parse_date
from glcc.ingest.contracts import classify_topic, extract_terms
from glcc.ingest.rulings import load_rulings
from glcc.ingest.vendors import VendorRegistry

DATA = Path(__file__).resolve().parent.parent / "data"
CORPUS = Corpus.load(DATA)


def clause(cid):
    c = CORPUS.clause(cid)
    assert c is not None, cid
    return c


class TestTopicClassification(unittest.TestCase):
    def test_notice_clause_mentioning_detention_is_a_notification_clause(self):
        # Regression: this was misclassified as detention_demurrage when the detention rule ran first.
        t = "ACME Freight must notify GLCC of any damage, detention, or delay within 48 hours of discovery."
        self.assertEqual(classify_topic(t), "notification_deadline")

    def test_detention_clause_needs_charge_allocation_context(self):
        self.assertEqual(classify_topic("Detention and demurrage charges are borne by the carrier."), "detention_demurrage")
        self.assertEqual(classify_topic("Detention of vessels is governed by port rules."), "other")

    def test_distractors_are_other(self):
        for cid in ("MSA-GLCC-001#2.1", "MSA-GLCC-001#3.2", "MSA-GLCC-001#3.3", "MSA-GLCC-001#4.2",
                    "MSA-GLCC-001#7.1", "VA-ACME-2022#8.1", "INS-POL-77#1.2"):
            self.assertEqual(clause(cid).topic, "other", cid)

    def test_real_topics(self):
        expected = {
            "MSA-GLCC-001#3.1": "liability_cap", "MSA-GLCC-001#4.1": "notification_deadline",
            "MSA-GLCC-001#5.1": "force_majeure", "MSA-GLCC-001#6.1": "customs_responsibility",
            "VA-ACME-2022#5.1": "liability_cap", "VA-ACME-2022#6.1": "notification_deadline",
            "VA-ACME-2022#7.1": "detention_demurrage", "INS-POL-77#1.1": "insurance_coverage",
            "VA-NORTHSTAR-2023#5.1": "notification_deadline",
        }
        for cid, topic in expected.items():
            self.assertEqual(clause(cid).topic, topic, cid)


class TestTermExtraction(unittest.TestCase):
    def test_liability_terms(self):
        self.assertEqual(clause("MSA-GLCC-001#3.1").terms, {"liability_cap": 50000.0, "per": "container"})
        self.assertEqual(clause("VA-ACME-2022#5.1").terms, {"liability_cap": 120000.0, "per": "shipment"})
        self.assertEqual(clause("VA-ACME-2022-AMD2#1.1").terms["liability_cap"], 80000.0)

    def test_hours(self):
        self.assertEqual(clause("MSA-GLCC-001#4.1").terms, {"notification_hours": 72})
        self.assertEqual(clause("VA-ACME-2022#6.1").terms, {"notification_hours": 48})
        self.assertEqual(clause("VA-NORTHSTAR-2023#5.1").terms, {"notification_hours": 36})

    def test_force_majeure_requires_written_notice(self):
        t = clause("MSA-GLCC-001#5.1").terms
        self.assertEqual(t["written_notice_hours"], 48)
        self.assertFalse(t["verbal_notice_sufficient"])

    def test_insurance_terms(self):
        t = clause("INS-POL-77#1.1").terms
        self.assertEqual((t["deductible"], t["coverage_percent"], t["late_notice_coverage_percent"], t["insurer_notice_hours"]),
                         (5000.0, 90.0, 50.0, 72))

    def test_detention_allocation(self):
        self.assertEqual(clause("VA-ACME-2022#7.1").terms, {"carrier_caused": "carrier", "documentation_error": "glcc"})

    def test_no_terms_for_text_without_numbers(self):
        self.assertEqual(extract_terms("liability_cap", "Liability is limited as agreed."), {})


class TestContractStructure(unittest.TestCase):
    def test_clause_ids_and_pages(self):
        self.assertEqual(clause("MSA-GLCC-001#3.1").page, 1)
        self.assertEqual(clause("MSA-GLCC-001#4.1").page, 2)       # the long agreement spans two pages

    def test_supersession_and_applicability_parsed(self):
        amd = clause("VA-ACME-2022-AMD2#1.1")
        self.assertEqual(amd.supersedes, ("VA-ACME-2022#5.1",))
        self.assertEqual((amd.effective_date, amd.applies_from), ("2024-03-01", "2024-03-01"))
        self.assertEqual(clause("VA-ACME-2022#5.1").supersedes, ())

    def test_both_northstar_addenda_replace_the_same_clause(self):
        a, b = clause("ADD-NORTHSTAR-2024-A#1.1"), clause("ADD-NORTHSTAR-2024-B#1.1")
        self.assertEqual(a.supersedes, b.supersedes)
        self.assertEqual(a.effective_date, b.effective_date)
        self.assertNotEqual(a.terms["liability_cap"], b.terms["liability_cap"])

    def test_scope_and_vendor(self):
        self.assertEqual((clause("MSA-GLCC-001#3.1").scope, clause("MSA-GLCC-001#3.1").vendor), ("general", "ALL"))
        self.assertEqual((clause("VA-ACME-2022#5.1").scope, clause("VA-ACME-2022#5.1").vendor), ("vendor_specific", "ACME Freight"))
        self.assertEqual(clause("INS-POL-77#1.1").scope, "general")

    def test_page_footers_do_not_leak_into_clause_text(self):
        for c in CORPUS.clauses:
            self.assertNotRegex(c.text, r"\bPage \d+\b", c.clause_id)

    def test_wrapped_lines_are_rejoined(self):
        self.assertIn("per shipment for cargo shipped on or after 1 March 2024", clause("VA-ACME-2022-AMD2#1.1").text)


class TestUnreadableAndBrokenDocuments(unittest.TestCase):
    def test_scanned_contract_is_recorded_not_parsed(self):
        self.assertEqual([u["doc_id"] for u in CORPUS.unreadable_documents], ["VA-ZENITH-2024"])
        self.assertEqual(CORPUS.unreadable_documents[0]["vendor"], "Zenith Cargo")
        self.assertFalse([c for c in CORPUS.clauses if c.doc_id == "VA-ZENITH-2024"])
        self.assertTrue(any("VA-ZENITH-2024" in w for w in CORPUS.warnings))

    def test_corrupt_pdf_does_not_stop_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(DATA, Path(tmp) / "d")
            (Path(tmp) / "d" / "contracts" / "BROKEN.pdf").write_bytes(b"this is not a pdf")
            corpus = Corpus.load(Path(tmp) / "d")
            self.assertEqual(len(corpus.clauses), len(CORPUS.clauses))
            self.assertTrue(any("BROKEN.pdf" in w for w in corpus.warnings))

    def test_missing_data_directory(self):
        with self.assertRaises(IngestError):
            Corpus.load("/no/such/dir")


class TestRegulations(unittest.TestCase):
    def sec(self, sid):
        s = CORPUS.reg_section(sid)
        assert s is not None, sid
        return s

    def test_version_windows(self):
        v1, v2 = self.sec("PAN-REG-003.v1#2"), self.sec("PAN-REG-003.v2#2")
        self.assertEqual((v1.effective_date, v1.valid_until), ("2022-03-01", "2025-01-15"))
        self.assertEqual((v2.effective_date, v2.valid_until), ("2025-01-15", None))

    def test_in_force_boundaries(self):
        v1, v2 = self.sec("PAN-REG-003.v1#2"), self.sec("PAN-REG-003.v2#2")
        self.assertTrue(v1.in_force("2024-11-04"))
        self.assertFalse(v2.in_force("2024-11-04"))
        self.assertFalse(v1.in_force("2025-01-15"))            # valid_until is exclusive
        self.assertTrue(v2.in_force("2025-01-15"))
        self.assertFalse(v1.in_force("2022-02-28"))

    def test_deadline_terms(self):
        self.assertEqual(self.sec("PAN-REG-003.v1#2").terms["submission_business_days"], 10)
        self.assertEqual(self.sec("PAN-REG-003.v2#2").terms["submission_business_days"], 5)
        self.assertEqual(self.sec("PAN-REG-003.v2#3").terms["release_business_days"], 2)
        self.assertIn("certificate of origin", self.sec("PAN-REG-003.v2#1").terms["required_documents"])

    def test_malformed_regulation_is_skipped_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(DATA, Path(tmp) / "d")
            (Path(tmp) / "d" / "regulations" / "BAD.v1.txt").write_text("Regulation ID: X\nno other fields\n")
            corpus = Corpus.load(Path(tmp) / "d")
            self.assertEqual(len(corpus.reg_sections), len(CORPUS.reg_sections))
            self.assertTrue(any("BAD.v1.txt" in w for w in corpus.warnings))


class TestRulings(unittest.TestCase):
    def test_loaded(self):
        ids = {r.ruling_id for r in CORPUS.rulings}
        self.assertIn("RULING-2023-014", ids)
        self.assertEqual(len(ids), len(CORPUS.rulings))

    def test_malformed_line_reports_line_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.jsonl"
            p.write_text('{"ruling_id": "R1"}\n')
            with self.assertRaises(IngestError) as ctx:
                load_rulings(p)
            self.assertIn("line 1", str(ctx.exception))
            p.write_text("{not json}\n")
            with self.assertRaises(IngestError):
                load_rulings(p)


class TestVendorRegistry(unittest.TestCase):
    def test_aliases_and_suffixes_normalize(self):
        reg = CORPUS.vendors
        for raw in ("ACME", "acme freight", "Acme Freight Ltd", "ACME Freight Ltd.", "acme-freight"):
            self.assertEqual(reg.canonical(raw), "ACME Freight", raw)
        self.assertEqual(reg.canonical("Northstar Lines Inc"), "Northstar Lines")

    def test_unknown_vendor(self):
        self.assertIsNone(CORPUS.vendors.canonical("Mystery Carrier"))

    def test_key_lookup(self):
        self.assertEqual(CORPUS.vendors.by_key("zenith"), "Zenith Cargo")


class TestDates(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_date("shipped on or after 1 March 2024"), "2024-03-01")
        self.assertEqual(parse_date("on 2024-05-20 at 08:00"), "2024-05-20")
        self.assertIsNone(parse_date("no date here"))
        self.assertIsNone(parse_date("31 Smarch 2024"))


class TestCaseLoading(unittest.TestCase):
    def test_documents_by_kind(self):
        case = CORPUS.case("CASE-1001")
        kinds = {d.doc_id: d.kind for d in case.documents}
        self.assertEqual(kinds["email_01_carrier_notice"], "email")
        self.assertEqual(kinds["chat_claims"], "chat")
        self.assertEqual(kinds["survey_independent"], "survey")
        self.assertEqual(kinds["attachment_claim_form"], "claim_form")
        self.assertEqual(kinds["photo_damage"], "image")

    def test_email_headers_are_parsed_to_utc(self):
        email = next(d for d in CORPUS.case("CASE-1001").documents if d.doc_id == "email_01_carrier_notice")
        self.assertEqual(email.meta["date"], "2024-05-21 09:00")
        self.assertIn("formally notifying", email.text)

    def test_pdf_attachment_text_is_extracted(self):
        form = next(d for d in CORPUS.case("CASE-1001").documents if d.kind == "claim_form")
        self.assertTrue(form.readable)
        self.assertIn("USD 95,000", form.text)

    def test_image_is_flagged_unreadable_not_ignored(self):
        photo = next(d for d in CORPUS.case("CASE-1001").documents if d.kind == "image")
        self.assertFalse(photo.readable)
        self.assertIn("OCR", photo.warnings[0])

    def test_vendor_alias_normalized_on_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(DATA, Path(tmp) / "d")
            p = Path(tmp) / "d" / "cases" / "CASE-1001" / "case.json"
            rec = json.loads(p.read_text())
            rec["vendor"] = "Acme Freight Ltd."
            p.write_text(json.dumps(rec))
            case = Corpus.load(Path(tmp) / "d").case("CASE-1001")
            self.assertEqual((case.vendor, case.vendor_raw), ("ACME Freight", "Acme Freight Ltd."))

    def test_all_sample_cases_load(self):
        self.assertEqual(len(CORPUS.case_ids()), 11)
        for cid in CORPUS.case_ids():
            self.assertEqual(CORPUS.case(cid).case_id, cid)

    def test_unknown_case_and_path_traversal(self):
        for bad in ("CASE-9999", "../data", "..", "CASE-1001/../CASE-1002", ""):
            with self.assertRaises(IngestError, msg=bad):
                CORPUS.case(bad)

    def test_bad_case_files_raise_clear_errors(self):
        cases = [
            ("{not json", "not valid JSON"),
            ('{"case_id": "X"}', "missing"),
            ('{"case_id":"X","case_type":"teleport","vendor":"v","shipment_date":"2024-01-01","port":"p","claim_amount":1}', "unknown case_type"),
            ('{"case_id":"X","case_type":"freight_damage","vendor":"v","shipment_date":"yesterday","port":"p","claim_amount":1}', "shipment_date"),
            ('{"case_id":"X","case_type":"freight_damage","vendor":"v","shipment_date":"2024-01-01","port":"p","claim_amount":-5}', "negative"),
            ('{"case_id":"X","case_type":"freight_damage","vendor":"v","shipment_date":"2024-01-01","port":"p","claim_amount":"lots"}', "number"),
        ]
        for content, expect in cases:
            with tempfile.TemporaryDirectory() as tmp:
                shutil.copytree(DATA, Path(tmp) / "d")
                folder = Path(tmp) / "d" / "cases" / "CASE-X"
                folder.mkdir()
                (folder / "case.json").write_text(content)
                with self.assertRaises(IngestError, msg=content) as ctx:
                    Corpus.load(Path(tmp) / "d").case("CASE-X")
                self.assertIn(expect, str(ctx.exception))

    def test_corrupt_attachment_does_not_fail_the_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(DATA, Path(tmp) / "d")
            (Path(tmp) / "d" / "cases" / "CASE-1001" / "attachment_corrupt.pdf").write_bytes(b"garbage")
            case = Corpus.load(Path(tmp) / "d").case("CASE-1001")
            bad = next(d for d in case.documents if d.doc_id == "attachment_corrupt")
            self.assertFalse(bad.readable)


if __name__ == "__main__":
    unittest.main()
