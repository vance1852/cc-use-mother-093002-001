import unittest

from digital_trade_foundation.meeting_acceptance import run


class MeetingAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertGreaterEqual(len(result["checks"]), 15)


if __name__ == "__main__":
    unittest.main()
