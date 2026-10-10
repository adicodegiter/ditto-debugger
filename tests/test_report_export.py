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
        self.assertIn("| 88 |", report)
        self.assertIn("not scored by design", report)
        self.assertIn("## Remediation Retest", report)
        search_latency_row = next(line for line in report.splitlines() if line.startswith("| Search latency p95 |"))
        self.assertIn("not recorded [run:", search_latency_row)
        self.assertIn("not demonstrated |", search_latency_row)
        self.assertNotIn("not recorded s", report)
        self.assertIn("## Evidence Gaps", report)
        self.assertIn("## Source Files", report)
        # Nothing the fixture data does not show: no deadline note without a burst run, no improvement claim.
        self.assertNotIn("Fixed during testing", report)
        self.assertIn("Conclusion: **not demonstrated**.", report)
        self.assertNotIn("kept their rate", report)

    def test_report_from_committed_evidence_names_weaknesses_and_traces_every_number(self):
        import subprocess, sys, tempfile
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "report.md"
            subprocess.run([sys.executable, "-m", "ai.report_export", "--out", str(out)], cwd=ROOT, check=True, capture_output=True)
            report = out.read_text(encoding="utf-8")
        from ai.findings import trace_check_markdown
        self.assertTrue(trace_check_markdown(report))
        self.assertIn("### 1. [HIGH] Search does not fail fast while things-search is unavailable", report)
        self.assertIn("[MEDIUM] Recovery after a things-search fault is slow and uneven", report)
        self.assertIn("**83.69 / 100**", report)
        self.assertIn("client-side improvement demonstrated; server-side fail-fast not demonstrated", report)
        self.assertIn("| Wait for one failed search | 10 s [run: run-c8b30b07c9f3] | 3 s [run: run-a09cb1bcbb7c] | -7 s |", report)
        self.assertIn("**Live record:**", report)  # the real forced-test record, not the reconstruction
        self.assertNotIn("data/experiments/", report)  # every source is committed
        self.assertNotIn("0.8888888888888888", report)


if __name__ == "__main__":
    unittest.main()