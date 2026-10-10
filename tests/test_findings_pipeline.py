from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai.findings import build_findings_for_run, build_report_markdown, trace_check_markdown

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


if __name__ == "__main__":
    unittest.main()
