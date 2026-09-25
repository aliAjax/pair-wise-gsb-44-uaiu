import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, PrivacyRequestService  # noqa: E402


class ExportReviewFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = PrivacyRequestService(Path(self.tmp.name) / "test.db")
        self.service.configure_jurisdiction("sup1", "supervisor", "CN", "中国", 30, 30, True, True)
        self.subject = self.service.create_subject("intake1", "intake", "SUB-EXP", "CN", False, "p@example.test")

    def tearDown(self):
        self.tmp.cleanup()

    def make_location(self, tag, subject=None):
        subject = subject or self.subject
        requester = "guardian" if subject["is_minor"] else "self"
        req = self.service.create_request(
            "intake1", "intake", "PR-E" + tag, subject["id"], "access", "IDEM-E" + tag, requester,
        )["request"]
        req = self.service.verify_identity("officer1", "privacy_officer", req["id"], req["version"], "ID-" + tag)
        req = self.service.assign_request("sup1", "supervisor", req["id"], "officer1", req["version"])
        location = self.service.add_data_location("officer1", "privacy_officer", req["id"], "CRM-" + tag, "profile", "customer")
        return req, location

    def test_high_risk_gaps_and_resubmit_keeps_number(self):
        _, location = self.make_location("HR")
        first = self.service.submit_export_review("officer1", "privacy_officer", location["id"], "US", "Analytics Inc.")
        self.assertFalse(first["reused"])
        self.assertEqual(1, first["review"]["high_risk"])
        self.assertFalse(first["gate"]["sendable"])
        self.assertIn("高风险目的地缺标准合同编号", first["gate"]["gaps"])
        self.assertIn("高风险目的地缺影响评估编号", first["gate"]["gaps"])
        with self.assertRaises(DomainError) as ctx:
            self.service.send_export("officer1", "privacy_officer", first["review"]["id"])
        self.assertEqual(409, ctx.exception.status)
        self.assertIn("发送入口已关闭", str(ctx.exception))
        again = self.service.submit_export_review(
            "officer1", "privacy_officer", location["id"], "US", "Analytics Inc.",
            standard_contract_no="SCC-2026-001", impact_assessment_no="PIA-2026-001",
        )
        self.assertTrue(again["reused"])
        self.assertEqual(first["review"]["review_no"], again["review"]["review_no"])
        self.assertTrue(again["gate"]["sendable"])
        sent = self.service.send_export("officer1", "privacy_officer", first["review"]["id"])
        self.assertTrue(sent["review"]["sent_at"])
        with self.assertRaises(DomainError) as ctx2:
            self.service.send_export("officer1", "privacy_officer", first["review"]["id"])
        self.assertEqual(409, ctx2.exception.status)
        with self.assertRaises(DomainError) as ctx3:
            self.service.submit_export_review("officer1", "privacy_officer", location["id"], "US", "Analytics Inc.")
        self.assertEqual(409, ctx3.exception.status)

    def test_one_active_review_per_destination(self):
        req, location = self.make_location("DUP")
        first = self.service.submit_export_review("officer1", "privacy_officer", location["id"], "DE", "Recv A")
        self.assertEqual(0, first["review"]["high_risk"])
        self.assertTrue(first["gate"]["sendable"])
        second = self.service.submit_export_review("officer1", "privacy_officer", location["id"], "DE", "Recv B")
        self.assertTrue(second["reused"])
        self.assertEqual(first["review"]["review_no"], second["review"]["review_no"])
        self.assertEqual("Recv B", second["review"]["recipient"])
        other = self.service.submit_export_review("officer1", "privacy_officer", location["id"], "JP", "Recv C")
        self.assertFalse(other["reused"])
        self.assertNotEqual(first["review"]["review_no"], other["review"]["review_no"])
        detail = self.service.get_request("sup1", "supervisor", req["id"])
        self.assertEqual(2, len(detail["export_reviews"]))

    def test_legal_hold_closes_send_entry(self):
        _, location = self.make_location("LH")
        self.service.classify_location("officer1", "privacy_officer", location["id"], False, True, False)
        review = self.service.submit_export_review("officer1", "privacy_officer", location["id"], "DE", "Recv")
        self.assertFalse(review["gate"]["sendable"])
        self.assertIn("法律保留未解除", review["gate"]["gaps"])
        with self.assertRaises(DomainError) as ctx:
            self.service.send_export("officer1", "privacy_officer", review["review"]["id"])
        self.assertEqual(409, ctx.exception.status)

    def test_minor_needs_guardian_consent(self):
        minor = self.service.create_subject("intake1", "intake", "SUB-EXP-M", "CN", True, "m@example.test")
        _, location = self.make_location("M", subject=minor)
        review = self.service.submit_export_review("officer1", "privacy_officer", location["id"], "DE", "Recv")
        self.assertFalse(review["gate"]["sendable"])
        self.assertIn("未成年人缺监护人同意", review["gate"]["gaps"])
        fixed = self.service.submit_export_review(
            "officer1", "privacy_officer", location["id"], "DE", "Recv", guardian_consent_ref="GC-9",
        )
        self.assertTrue(fixed["reused"])
        self.assertEqual(review["review"]["review_no"], fixed["review"]["review_no"])
        self.assertTrue(fixed["gate"]["sendable"])
        sent = self.service.send_export("sup1", "supervisor", review["review"]["id"])
        self.assertTrue(sent["review"]["sent_at"])

    def test_withdraw_consent_deactivates_but_keeps_history(self):
        req, location = self.make_location("WD")
        us = self.service.submit_export_review(
            "officer1", "privacy_officer", location["id"], "US", "Analytics Inc.",
            standard_contract_no="SCC-1", impact_assessment_no="PIA-1",
        )
        de = self.service.submit_export_review("officer1", "privacy_officer", location["id"], "DE", "Recv")
        result = self.service.withdraw_subject_consent("officer1", "privacy_officer", self.subject["id"], "用户邮件撤回")
        self.assertEqual(2, len(result["deactivated_reviews"]))
        detail = self.service.get_request("sup1", "supervisor", req["id"])
        self.assertEqual(2, len(detail["export_reviews"]))
        for item in detail["export_reviews"]:
            self.assertEqual("deactivated", item["status"])
            self.assertFalse(item["gate"]["sendable"])
            self.assertIn("同意已撤回，审查已停用", item["gate"]["gaps"])
        us_view = [i for i in detail["export_reviews"] if i["review_no"] == us["review"]["review_no"]][0]
        self.assertEqual("SCC-1", us_view["standard_contract_no"])
        self.assertEqual("PIA-1", us_view["impact_assessment_no"])
        with self.assertRaises(DomainError) as ctx:
            self.service.send_export("officer1", "privacy_officer", de["review"]["id"])
        self.assertEqual(409, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.withdraw_subject_consent("officer1", "privacy_officer", self.subject["id"])
        self.assertEqual(409, ctx2.exception.status)
        state = self.service.state("sup1", "supervisor")
        self.assertEqual(2, len(state["export_reviews"]))
        fresh = self.service.submit_export_review("officer1", "privacy_officer", location["id"], "US", "Analytics Inc.")
        self.assertFalse(fresh["reused"])
        self.assertNotEqual(us["review"]["review_no"], fresh["review"]["review_no"])

    def test_permissions(self):
        _, location = self.make_location("PM")
        with self.assertRaises(DomainError) as ctx:
            self.service.submit_export_review("intake1", "intake", location["id"], "DE", "Recv")
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.submit_export_review("other", "privacy_officer", location["id"], "DE", "Recv")
        self.assertEqual(403, ctx2.exception.status)
        review = self.service.submit_export_review("officer1", "privacy_officer", location["id"], "DE", "Recv")
        with self.assertRaises(DomainError) as ctx3:
            self.service.send_export("intake1", "intake", review["review"]["id"])
        self.assertEqual(403, ctx3.exception.status)
        with self.assertRaises(DomainError) as ctx4:
            self.service.withdraw_subject_consent("intake1", "intake", self.subject["id"])
        self.assertEqual(403, ctx4.exception.status)


if __name__ == "__main__":
    unittest.main()
