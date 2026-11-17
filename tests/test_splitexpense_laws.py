"""Lean checks HTTP observations, including deliberate balanced corruption."""
import unittest

from splitexpense.laws import run_suite


class SplitExpenseLawTests(unittest.TestCase):
    def test_real_http_histories_conform_to_lean_and_corruption_is_detected(self):
        report = run_suite(seed=7, groups=3)
        self.assertEqual(report["status"], "passed", report)
        self.assertEqual(report["evidence"], "sampled_http_sequences")
        self.assertFalse(report["implementation_proved"])
        self.assertEqual(report["cases_checked"], 54)
        self.assertGreater(report["eligible_cases"], 0)
        self.assertGreater(report["rejection_cases"], 0)
        self.assertIn("409", report["http_statuses"])
        self.assertIn("400", report["http_statuses"])
        self.assertEqual(len(report["negative_probes"]), 2)
        self.assertTrue(all(probe["rejected"] for probe in report["negative_probes"]))

    def test_zero_observation_plan_is_not_success(self):
        with self.assertRaises(ValueError):
            run_suite(groups=0)


if __name__ == "__main__":
    unittest.main()
