from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from monitor.sli import baseline_summary, summarize_freshness_samples, summarize_metrics


class MeasurementSliTests(unittest.TestCase):
    def test_measurement_summary_accepts_write_search_and_freshness_samples(self):
        records = [
            {"kind": "write", "success": True, "latency_ms": 120.0},
            {"kind": "write", "success": True, "latency_ms": 180.0},
            {"kind": "write", "success": False, "latency_ms": 400.0},
            {"kind": "search", "success": True, "latency_ms": 240.0},
            {"kind": "search", "success": True, "latency_ms": 260.0},
            {"kind": "freshness", "observed_at": "2026-10-10T13:00:00Z"},
            {"kind": "freshness", "observed_at": "2026-10-10T13:00:10Z"},
        ]
        summary = summarize_metrics(records)
        self.assertEqual(summary["status"], "unhealthy")
        self.assertEqual(summary["write"]["status"], "degraded")
        self.assertEqual(summary["search"]["status"], "healthy")
        self.assertIn("freshness", summary["freshness"]["kind"])

    def test_freshness_summary_uses_seconds_and_marks_stale_window(self):
        summary = summarize_freshness_samples([
            {"observed_at": "2026-10-10T13:00:00Z", "freshness_seconds": 3.0},
            {"observed_at": "2026-10-10T13:00:20Z", "freshness_seconds": 8.0},
        ])
        self.assertIn("p95_seconds", summary)
        self.assertEqual(summary["p95_seconds"], 8.0)
        self.assertIn(summary["status"], {"healthy", "degraded"})

    def test_jsonl_baseline_summary_handles_missing_kinds(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "write.jsonl").write_text('{"kind": "write", "success": true, "latency_ms": 100}\n', encoding="utf-8")
            summary = baseline_summary(base)
        self.assertEqual(summary["status"], "unhealthy")
        self.assertTrue(any("Missing" in warning for warning in summary["warnings"]))


if __name__ == "__main__":
    unittest.main()
