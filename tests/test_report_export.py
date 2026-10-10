from __future__ import annotations

import json
import unittest
from pathlib import Path

from ai.findings import build_ranked_findings
from ai.report_export import build_final_report
from ai.retest import compare_retest_runs


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FinalReportExportTests(unittest.TestCase):
    def test_report_renders_fixture_labels_scores_coverage_and_retest(self):
        run_path = FIXTURES / "findings-cli-pause.json"
        run = _load_fixture(run_path.name)
        diagnosis = run["diagnosis"]
        findings = build_ranked_findings([(run, run_path)])
        before = _load_fixture("restart-retest-before.json")
        after = _load_fixture("restart-retest-after.json")
        retest = compare_retest_runs(before, after)

        report = build_final_report(
            [run],
            {run["run_id"]: diagnosis},
            findings,
            retest,
            _load_fixture("m4-forced-test-docs-reconstruction.json"),
            source_labels={"fixture": "Fixture sources only in this test."},
        )

        self.assertIn("## Fault-Type Coverage", report)
        self.assertIn("| CPU throttle | NOT RUN | none |", report)
        self.assertIn("## Resilience Scores", report)
        self.assertIn("88.0", report)
        self.assertIn("not scored by design", report)
        self.assertIn("## Remediation Retest", report)
        search_latency_row = next(line for line in report.splitlines() if line.startswith("| Search latency p95 |"))
        self.assertIn("not recorded [run:", search_latency_row)
        self.assertIn("not demonstrated |", search_latency_row)
        self.assertNotIn("not recorded s", report)
        self.assertIn("## Evidence Gaps", report)
        self.assertIn("## Source Files", report)


if __name__ == "__main__":
    unittest.main()