from __future__ import annotations

import json
import unittest
from pathlib import Path

from ai.retest import compare_retest_runs


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class RetestComparisonTests(unittest.TestCase):
    def test_comparison_reports_conditions_metrics_and_baseline_disagreement(self):
        result = compare_retest_runs(
            _fixture("restart-retest-before.json"),
            _fixture("restart-retest-after.json"),
        )

        self.assertEqual(result["before_run_id"], "run-c8b30b07c9f3")
        self.assertEqual(result["after_run_id"], "run-a09cb1bcbb7c")
        self.assertTrue(result["conditions"]["same_fault"])
        self.assertTrue(result["conditions"]["same_target"])
        self.assertTrue(result["conditions"]["same_duration"])
        self.assertEqual(result["conditions"]["client_timeout_seconds"]["delta"], -7.0)
        self.assertEqual(result["conditions"]["baseline_write_p95_ms"]["delta"], 8.932)
        self.assertEqual(result["conditions"]["baseline_ids"]["before"], None)
        self.assertIsNone(result["conditions"]["baseline_ids"]["after"])
        self.assertEqual(
            result["conditions"]["baseline_ids"]["after_reported_in_summary"],
            "baseline-c290fc21633a",
        )
        self.assertEqual(result["measurements"]["search_failure_wait_seconds"]["delta"], -7.0)
        self.assertEqual(result["measurements"]["last_search_failure_after_rollback_seconds"]["delta"], 5.0)
        self.assertEqual(result["measurements"]["search_request_timeouts"]["delta_count"], 3.0)
        self.assertEqual(result["measurements"]["write_error_rate"]["delta"], 0.0)
        self.assertEqual(result["measurements"]["write_p95_ms"]["delta"], 18.586)
        self.assertEqual(result["measurements"]["recovery_time_seconds"]["delta"], 18.171)
        self.assertEqual(result["measurements"]["search_outage_seconds"]["conclusion"], "not demonstrated")
        self.assertEqual(result["measurements"]["search_latency_p95_ms"]["conclusion"], "not demonstrated")
        self.assertEqual(result["overall_improvement"], "not demonstrated")

    def test_missing_values_remain_not_demonstrated(self):
        before = {"run_id": "before", "observations": []}
        after = {"run_id": "after", "observations": []}

        result = compare_retest_runs(before, after)

        self.assertEqual(result["conditions"]["same_fault"], None)
        self.assertEqual(result["measurements"]["search_outage_seconds"]["conclusion"], "not demonstrated")
        self.assertEqual(result["measurements"]["write_p95_ms"]["delta"], None)


if __name__ == "__main__":
    unittest.main()