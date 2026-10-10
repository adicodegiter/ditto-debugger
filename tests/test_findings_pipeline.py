from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai.findings import build_findings_for_run, build_ranked_findings, build_report_markdown, trace_check_markdown

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures"


class FindingsPipelineTests(unittest.TestCase):
    def test_build_findings_for_run_from_fixture(self):
        raw = json.loads((FIXTURE_DIR / "m4-normal-docs-reconstruction.json").read_text(encoding="utf-8"))
        raw["score_components"] = {"availability": 1.0, "latency": 0.9, "recovery": 1.0, "containment": 0.7, "detection": 0.8}
        run = build_findings_for_run(raw)
        self.assertIn("title", run)
        self.assertEqual(run["status"], "confirmed")
        self.assertIn(run["severity"], {"critical", "high", "medium", "low"})
        self.assertIn("hypothesis", run)
        self.assertIn("validation_test", run)
        self.assertTrue(run["evidence_ids"])

    def test_build_report_uses_placeholder_when_fault_type_missing(self):
        out = build_report_markdown([
            {"run_id": "run-a", "fault_type": "restart_container", "target": "things-search", "score_components": {"availability": 1.0, "latency": 1.0, "recovery": 1.0, "containment": 1.0, "detection": 1.0}, "status": "completed"}
        ], policy_path=ROOT / "configs" / "experiment-policy.yaml")
        self.assertIn("NOT RUN YET", out)
        self.assertIn("Fault-type coverage", out)

    def test_trace_check_rejects_untagged_number(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.md"
            path.write_text("This line contains 7 and no tag.\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "tag"):
                trace_check_markdown(path.read_text(encoding="utf-8"), known_run_ids=set())

    def test_forced_runs_are_excluded_from_regular_results(self):
        raw = json.loads((FIXTURE_DIR / "m4-forced-test-docs-reconstruction.json").read_text(encoding="utf-8"))
        run = build_findings_for_run(raw)
        self.assertEqual(run["status"], "inconclusive")
        self.assertIn("not scored", run["title"].lower())

    def test_ranked_findings_include_only_real_runs_with_windowed_claim_evidence(self):
        real_path = FIXTURE_DIR / "findings-cli-pause.json"
        forced_path = FIXTURE_DIR / "findings-cli-forced.json"
        records = [
            (json.loads(real_path.read_text(encoding="utf-8")), real_path),
            (json.loads(forced_path.read_text(encoding="utf-8")), forced_path),
        ]

        report = build_ranked_findings(records)

        self.assertEqual(report["real_run_count"], 1)
        self.assertEqual(report["distinct_fault_type_count"], 1)
        self.assertEqual(report["fault_types_with_real_data"], ["pause"])
        self.assertGreaterEqual(len(report["ranked_findings"]), 1)
        self.assertTrue(report["informational_results"])
        self.assertTrue(all(item["severity"] == "info" for item in report["informational_results"]))
        for finding in report["ranked_findings"]:
            for claim in finding["measured_claims"]:
                self.assertEqual(claim["run_id"], "run-fixture-pause")
                self.assertEqual(claim["source_file"], "tests/fixtures/findings-cli-pause.json")
                self.assertEqual(claim["metric_window"]["start"], "2026-10-10T17:10:52Z")
                self.assertEqual(claim["metric_window"]["end"], "2026-10-10T17:11:22Z")
        self.assertTrue(any("No real CPU throttle run" in gap for gap in report["report_gaps"]))

    def test_claim_without_a_complete_metric_window_is_printable_and_dropped(self):
        path = FIXTURE_DIR / "findings-cli-pause.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["observations"][0].pop("window_end")

        report = build_ranked_findings([(raw, path)])

        self.assertEqual(report["ranked_findings"], [])
        self.assertTrue(any("Dropped unsupported claim" in warning and "metric_window.end" in warning
                            for warning in report["dropped_claims"]))


if __name__ == "__main__":
    unittest.main()
