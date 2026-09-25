import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ExportReviewService, PrivacyRequestService, build_export_materials, evaluate_export  # noqa: E402


class ExportReviewFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = Path(self.tmp.name) / "test.db"
        self.service = PrivacyRequestService(db)
        self.export = ExportReviewService(db)
        self.service.configure_jurisdiction("sup1", "supervisor", "CN", "中国", 30, 30, True, True)
        self.export.configure_destination("sup1", "supervisor", "SG", "新加坡节点", "SG", "standard")
        self.export.configure_destination("sup1", "supervisor", "US-HIGH", "美国分析节点", "US", "high")
        self.subject = self.service.create_subject("intake1", "intake", "SUB-001", "CN", False, "adult@example.test")
        self.request = self._open_request("PR-X1", self.subject)

    def tearDown(self):
        self.tmp.cleanup()

    def _open_request(self, number, subject, requester="self"):
        request = self.service.create_request(
            "intake1", "intake", number, subject["id"], "access", "IDEM-" + number, requester)["request"]
        request = self.service.verify_identity("officer1", "privacy_officer", request["id"], request["version"], "ID-1")
        request = self.service.assign_request("sup1", "supervisor", request["id"], "officer1", request["version"])
        return request

    def _stamp(self, system, destination, recipient, legal_hold=False):
        loc = self.service.add_data_location("officer1", "privacy_officer", self.request["id"], system, "profile", "customer")
        loc = self.service.classify_location("officer1", "privacy_officer", loc["id"], False, legal_hold, False)
        return self.service.register_location_destination(
            "officer1", "privacy_officer", loc["id"], destination, recipient, loc["version"])

    def test_pure_decision_high_risk_requires_both_references(self):
        materials = {"legal_hold_location_ids": [], "is_minor": False, "guardian_consent": False,
                     "consent_status": "none", "risk_level": "high"}
        blocked = evaluate_export(materials, None, None)
        self.assertEqual("blocked", blocked["decision"])
        self.assertEqual({"missing_standard_contract_no", "missing_assessment_no"}, set(blocked["gaps"]))
        approved = evaluate_export(materials, "SCC-1", "PIA-1")
        self.assertTrue(approved["send_enabled"])

    def test_standard_destination_approved_and_gate_closed_until_routed(self):
        gate = self.export.gate("sup1", "supervisor", self.request["id"])
        self.assertFalse(gate["send_enabled"])
        self._stamp("CRM", "SG", "Acme SG")
        review = self.export.submit_review("officer1", "privacy_officer", self.request["id"], "SG", "Acme SG")
        self.assertEqual("approved", review["status"])
        self.assertEqual([], review["gaps"])
        gate = self.export.gate("sup1", "supervisor", self.request["id"])
        self.assertTrue(gate["send_enabled"])

    def test_high_risk_gap_blocks_send_then_reuses_same_review_number(self):
        self._stamp("BI", "US-HIGH", "Acme US")
        blocked = self.export.submit_review("officer1", "privacy_officer", self.request["id"], "US-HIGH", "Acme US")
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual({"missing_standard_contract_no", "missing_assessment_no"}, set(blocked["gaps"]))
        self.assertFalse(self.export.gate("sup1", "supervisor", self.request["id"])["send_enabled"])
        half = self.export.submit_review("officer1", "privacy_officer", self.request["id"], "US-HIGH", "Acme US",
                                         "SCC-9", None, blocked["version"])
        self.assertEqual("ER-00001", blocked["review_no"])
        self.assertEqual(blocked["review_no"], half["review_no"])
        self.assertEqual(["missing_assessment_no"], half["gaps"])
        approved = self.export.submit_review("officer1", "privacy_officer", self.request["id"], "US-HIGH", "Acme US",
                                             "SCC-9", "PIA-9", half["version"])
        self.assertEqual(blocked["review_no"], approved["review_no"])
        self.assertEqual("approved", approved["status"])

    def test_only_one_active_review_per_destination_recipient(self):
        self._stamp("CRM", "SG", "Acme SG")
        first = self.export.submit_review("officer1", "privacy_officer", self.request["id"], "SG", "Acme SG")
        second = self.export.submit_review("officer1", "privacy_officer", self.request["id"], "SG", "Acme SG")
        self.assertEqual(first["id"], second["id"])
        detail = self.service.get_request("sup1", "supervisor", self.request["id"])
        active = [r for r in detail["export_reviews"] if r["status"] in ("approved", "blocked")]
        self.assertEqual(1, len(active))

    def test_legal_hold_blocks_even_with_contracts(self):
        self._stamp("ARCHIVE", "US-HIGH", "Acme US", legal_hold=True)
        review = self.export.submit_review("officer1", "privacy_officer", self.request["id"], "US-HIGH", "Acme US",
                                           "SCC-1", "PIA-1")
        self.assertIn("legal_hold", review["gaps"])
        self.assertEqual("blocked", review["status"])

    def test_minor_requires_guardian_consent(self):
        minor = self.service.create_subject("intake1", "intake", "SUB-MINOR", "CN", True, "minor@example.test")
        request = self._open_request("PR-XM", minor, requester="guardian")
        self.request = request
        loc = self.service.add_data_location("officer1", "privacy_officer", request["id"], "CRM", "profile", "customer")
        loc = self.service.classify_location("officer1", "privacy_officer", loc["id"], False, False, False)
        self.service.register_location_destination("officer1", "privacy_officer", loc["id"], "SG", "Acme SG", loc["version"])
        blocked = self.export.submit_review("officer1", "privacy_officer", request["id"], "SG", "Acme SG")
        self.assertIn("minor_guardian_consent", blocked["gaps"])
        self.export.grant_consent("intake1", "intake", minor["id"], "guardian")
        approved = self.export.submit_review("officer1", "privacy_officer", request["id"], "SG", "Acme SG",
                                             expected_version=blocked["version"])
        self.assertEqual("approved", approved["status"])
        self.assertEqual(blocked["review_no"], approved["review_no"])

    def test_withdraw_consent_deactivates_approved_but_history_remains(self):
        self._stamp("CRM", "SG", "Acme SG")
        review = self.export.submit_review("officer1", "privacy_officer", self.request["id"], "SG", "Acme SG")
        self.assertEqual("approved", review["status"])
        result = self.export.withdraw_consent("intake1", "intake", self.subject["id"])
        self.assertEqual([review["id"]], result["deactivated_review_ids"])
        history = self.export.get_review_history("sup1", "supervisor", review["id"])
        self.assertEqual("inactive", history["status"])
        self.assertEqual("consent_withdrawn", history["deactivated_reason"])
        self.assertIn("materials", history["basis"])
        gate = self.export.gate("sup1", "supervisor", self.request["id"])
        self.assertFalse(gate["send_enabled"])
        self.assertEqual("inactive", self.service.get_request("sup1", "supervisor", self.request["id"])["export_reviews"][0]["status"])

    def test_re_submission_after_withdrawal_gets_new_number_when_old_inactive(self):
        self._stamp("CRM", "SG", "Acme SG")
        first = self.export.submit_review("officer1", "privacy_officer", self.request["id"], "SG", "Acme SG")
        self.export.withdraw_consent("intake1", "intake", self.subject["id"])
        self.export.grant_consent("intake1", "intake", self.subject["id"], "self")
        second = self.export.submit_review("officer1", "privacy_officer", self.request["id"], "SG", "Acme SG")
        self.assertNotEqual(first["review_no"], second["review_no"])
        self.assertEqual("approved", second["status"])
        detail = self.service.get_request("sup1", "supervisor", self.request["id"])
        statuses = {r["review_no"]: r["status"] for r in detail["export_reviews"]}
        self.assertEqual("inactive", statuses[first["review_no"]])
        self.assertEqual("approved", statuses[second["review_no"]])

    def test_unknown_destination_and_unrouted_location_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            self.export.submit_review("officer1", "privacy_officer", self.request["id"], "ZZ", "Nobody")
        self.assertEqual(409, ctx.exception.status)
        self._stamp("CRM", "SG", "Acme SG")
        with self.assertRaises(DomainError) as ctx2:
            self.export.submit_review("officer1", "privacy_officer", self.request["id"], "SG", "Other Recipient")
        self.assertEqual(409, ctx2.exception.status)

    def test_permissions_and_unconfigured_location_registration(self):
        with self.assertRaises(DomainError):
            self.export.configure_destination("officer1", "privacy_officer", "JP", "日本", "JP", "standard")
        with self.assertRaises(DomainError) as ctx:
            self.export.submit_review("officer2", "privacy_officer", self.request["id"], "SG", "Acme SG")
        self.assertEqual(403, ctx.exception.status)

    def test_materials_builder_rejects_mismatched_destination(self):
        with self.assertRaises(DomainError):
            build_export_materials(1, "SG", "R", {"code": "US", "name": "u", "risk_level": "standard"},
                                   [], {"id": 1, "is_minor": 0}, None)


if __name__ == "__main__":
    unittest.main()
