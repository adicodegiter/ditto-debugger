from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import requests

from monitor.sli import baseline_summary, summarize_freshness_samples, summarize_metrics
from simulator.fleet_workload import _run_search_probe, _run_write_probe


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
        self.assertEqual(summary["status"], "healthy")

    def test_freshness_with_only_failed_samples_is_unknown(self):
        summary = summarize_freshness_samples([{"kind": "freshness", "success": False}])
        self.assertEqual(summary["status"], "unknown")

    def test_jsonl_baseline_summary_handles_missing_kinds(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "write.jsonl").write_text(
                '{"kind":"write","source":"live","run_id":"run-a","success":true,"latency_ms":100,"timestamp":"2026-10-10T13:00:00.000Z"}\n'
                '{"kind":"write","source":"fixture","run_id":"run-a","success":true,"latency_ms":100,"timestamp":"2026-10-10T13:00:01.000Z"}\n',
                encoding="utf-8",
            )
            summary = baseline_summary(base, run_id="run-a", window_start=datetime(2026, 10, 10, 13, 0, tzinfo=timezone.utc))
        self.assertEqual(summary["status"], "unhealthy")
        self.assertTrue(any("Missing" in warning for warning in summary["warnings"]))
        self.assertEqual(summary["write"]["samples"], 1)

    def test_probes_record_transport_failures_instead_of_raising(self):
        session = Mock()
        session.put.side_effect = requests.ConnectionError("gateway down")
        session.get.side_effect = requests.Timeout("search timeout")

        write = _run_write_probe(session, "bosch.fleet:vehicle-01", value=10, api_base="http://local/api/2", run_id="run-fail")
        search = _run_search_probe(session, "bosch.fleet:vehicle-01", api_base="http://local/api/2", run_id="run-fail")

        self.assertFalse(write["success"])
        self.assertIsNone(write["status_code"])
        self.assertEqual(write["error"], "ConnectionError")
        self.assertFalse(search["success"])
        self.assertIsNone(search["status_code"])
        self.assertEqual(search["error"], "Timeout")


if __name__ == "__main__":
    unittest.main()
