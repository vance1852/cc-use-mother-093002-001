import unittest

from digital_trade_foundation.scheduling_acceptance import run


class SchedulingAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["double_book_blocked"])
        self.assertTrue(result["rejection_explained"])
        self.assertEqual("procure", result["rejection_competing_session"])
        self.assertTrue(result["low_clearance_blocked"])
        self.assertTrue(result["material_open_before_delegate"])
        self.assertTrue(result["material_revoked_after_delegate"])
        self.assertTrue(result["delegate_access_allowed"])
        self.assertTrue(result["delegate_revocation_blocked_by_fact"])
        self.assertEqual(1, result["waitlist_rank"])
        self.assertEqual("p-co2", result["promoted_after_withdraw"])
        self.assertTrue(result["restart_preserved_audit"])
        self.assertTrue(result["reverse_lookup_ok"])


if __name__ == "__main__":
    unittest.main()
