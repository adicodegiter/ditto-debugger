from __future__ import annotations

import unittest

from ai.findings import diagnose_report_findings, enrich_findings


class P4FindingEnrichmentTests(unittest.TestCase):
    def test_one_real_report_finding_is_enriched_with_prediction_and_real_window(self):
        run = {
            "schema_version": "1.0",
            "run_id": "run-fixture-real",
            "experiment_id": "stop-things-search-fixture",
            "status": "completed",
            "created_at": "2026-10-10T12:00:00Z",
            "prediction": {"predicted_slis": {"write": "hold", "search": "fail", "freshness": "hold"}},
            "observations": [{
                "phase": "fault",
                "window_start": "2026-10-10T12:00:00Z",
                "window_end": "2026-10-10T12:01:00Z",
                "write_samples": 4,
                "write_error_rate": 0.0,
                "write_p95_ms": 36.0,
                "search_samples": 4,
                "search_error_rate": 1.0,
                "freshness_samples": 3,
                "freshness_p95_seconds": 1.1,
            }],
            "cleanup_state": "complete",
            "recovery_verified": True,
            "recovery_time_seconds": 3.7,
        }
        experiment = {
            "id": run["experiment_id"],
            "target_component": "things-search",
            "action": "stop_container",
        }
        baseline = {"slis": {
            "write": {"error_rate": 0.0, "p95_ms": 35.0},
            "search": {"error_rate": 0.0, "p95_ms": 60.0},
            "freshness": {"error_rate": 0.0, "p95_ms": 1.0},
        }}
        p3_report_findings = [{
            "schema_version": "1.0",
            "id": f"{run['run_id']}-availability",
            "run_id": run["run_id"],
            "severity": "medium",
            "summary": "Measured availability degradation during the fault window.",
            "evidence": [f"run:{run['run_id']}"],
            "recommendation": "Review the affected service dependency.",
            "confidence": 1.0,
            "ai_generated": False,
        }]

        enriched = diagnose_report_findings(p3_report_findings, run, experiment, baseline)

        self.assertEqual(len(enriched), 1)
        finding = enriched[0]
        self.assertEqual(finding["schema_version"], "1.1")
        self.assertEqual(finding["prediction_comparison"]["search"]["verdict"], "match")
        self.assertEqual(finding["evidence_status"], "experiment")
        self.assertEqual(finding["severity"], "medium")
        self.assertTrue(finding["recommendation"])
        self.assertEqual(finding["evidence_links"], [{
            "result_id": run["run_id"],
            "sli": "write",
            "metric_window": {"start": "2026-10-10T12:00:00Z", "end": "2026-10-10T12:01:00Z"},
        }, {
            "result_id": run["run_id"],
            "sli": "search",
            "metric_window": {"start": "2026-10-10T12:00:00Z", "end": "2026-10-10T12:01:00Z"},
        }, {
            "result_id": run["run_id"],
            "sli": "freshness",
            "metric_window": {"start": "2026-10-10T12:00:00Z", "end": "2026-10-10T12:01:00Z"},
        }])

    def test_inconclusive_enrichment_does_not_claim_success_or_failure(self):
        run = {"run_id": "run-fixture-gap", "observations": []}
        diagnosis = {"run_id": "run-fixture-gap", "comparison": {"search": {"verdict": "not_measured"}}, "evidence_use": "inconclusive"}
        finding = {"run_id": "run-fixture-gap", "severity": "high", "recommendation": "Prior recommendation."}

        result = enrich_findings([finding], run, diagnosis)

        self.assertEqual(result[0]["evidence_status"], "inconclusive")
        self.assertEqual(result[0]["severity"], "info")
        self.assertIn("Do not score", result[0]["recommendation"])
        self.assertEqual(result[0]["evidence_links"], [])

    def test_enrichment_rejects_cross_run_evidence(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            enrich_findings(
                [{"run_id": "run-a"}],
                {"run_id": "run-a", "observations": []},
                {"run_id": "run-b", "comparison": {}},
            )

    def test_measured_comparison_without_metric_window_is_inconclusive(self):
        run = {"run_id": "run-fixture-unbounded", "observations": [{"phase": "fault"}]}
        diagnosis = {
            "run_id": "run-fixture-unbounded",
            "comparison": {"search": {"predicted": "fail", "observed": "fail", "verdict": "match"}},
            "evidence_use": "experiment",
        }

        result = enrich_findings([{"run_id": run["run_id"], "severity": "medium"}], run, diagnosis)

        self.assertEqual(result[0]["evidence_status"], "inconclusive")
        self.assertEqual(result[0]["evidence_links"], [])

    def test_dry_run_and_forced_kill_switch_do_not_produce_findings(self):
        experiment = {"id": "exp-fixture", "target_component": "things-search", "action": "stop_container",
                      "predicted_slis": {"search": "fail"}}
        finding = {"run_id": "run-fixture-excluded"}
        dry_run = {"run_id": "run-fixture-excluded", "dry_run": True, "observations": []}
        forced = {
            "run_id": "run-fixture-excluded",
            "experiment_id": "exp-fixture",
            "status": "aborted",
            "created_at": "2026-10-10T12:00:00Z",
            "prediction": {},
            "observations": [{"kind": "kill_switch", "tripped": True, "forced_test": True}],
            "cleanup_state": "complete",
        }

        self.assertEqual(diagnose_report_findings([finding], dry_run, experiment), [])
        self.assertEqual(diagnose_report_findings([finding], forced, experiment), [])


if __name__ == "__main__":
    unittest.main()
