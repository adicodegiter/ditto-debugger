from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT))

from ai.diagnosis import classify, classify_freshness, diagnose, history_outcome, load_thresholds

DIAG_SCHEMA = json.loads((ROOT / "schemas" / "diagnosis.schema.json").read_text(encoding="utf-8"))
RUN_SCHEMA = json.loads((ROOT / "schemas" / "experiment-run.schema.json").read_text(encoding="utf-8"))
BASELINE = {"slis": {"write": {"error_rate": 0.0, "p95_ms": 36.0}, "search": {"error_rate": 0.0, "p95_ms": 60.0},
                     "freshness": {"error_rate": 0.0, "p95_ms": 40.0}}}
EXPERIMENT = {"id": "restart-things-search-x", "target_component": "things-search", "action": "restart_container",
              "predicted_slis": {"write": "hold", "search": "fail", "freshness": "hold"}}


def sli(name, error_rate, p95):
    return {"kind": "sli", "sli": name, "window": "during", "error_rate": error_rate, "p95_ms": p95}


def run(observations, **extra):
    base = {"schema_version": "1.0", "run_id": "run-1", "experiment_id": "restart-things-search-x", "status": "completed",
            "created_at": "2026-10-10T12:00:00Z", "prediction": {}, "observations": observations, "cleanup_state": "complete"}
    base.update(extra)
    return base


def runner_fixture(name):
    record = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    metadata = {"_fixture", "_source", "_reported_results"}
    return {key: value for key, value in record.items() if key not in metadata}, record


NORMAL = [sli("write", 0.0, 36.0), sli("search", 1.0, 3000.0), sli("freshness", 0.0, 40.0),
          {"kind": "recovery", "recovery_s": 38.0, "recovered": True}]


class P4DiagnosisTests(unittest.TestCase):
    def check(self, d, r):
        Draft202012Validator(DIAG_SCHEMA, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(d)
        Draft202012Validator(RUN_SCHEMA, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(r)

    def test_matching_run(self):
        r = run(NORMAL)
        d = diagnose(EXPERIMENT, r, BASELINE)
        self.check(d, r)
        self.assertEqual(d["prediction_accuracy"], 1.0)
        self.assertEqual(d["evidence_use"], "experiment")
        self.assertEqual(d["flags"], [])
        # Search failing exactly as predicted is an expected impact, not a failure for the planner history.
        self.assertEqual(history_outcome(d), "no_failure_observed")

    def test_worse_than_predicted_is_flagged(self):
        r = run([sli("write", 0.5, 900.0), sli("search", 1.0, 3000.0), sli("freshness", 0.0, 40.0),
                 {"kind": "recovery", "recovery_s": 38.0, "recovered": True}])
        d = diagnose(EXPERIMENT, r, BASELINE)
        self.assertEqual(d["comparison"]["write"]["verdict"], "worse_than_predicted")
        self.assertIn("worse_than_predicted", d["flags"])
        self.assertEqual(history_outcome(d), "failure_observed")

    def test_missing_sli_is_not_measured_never_assumed_healthy(self):
        r = run([sli("search", 1.0, 3000.0), {"kind": "recovery", "recovery_s": 20, "recovered": True}])
        d = diagnose(EXPERIMENT, r, BASELINE)
        self.assertEqual(d["comparison"]["write"]["verdict"], "not_measured")
        self.assertEqual(d["comparison"]["freshness"]["observed"], "not_measured")
        self.assertIn("Not measured", d["summary"])

    def test_no_measurements_is_inconclusive_and_not_recorded(self):
        d = diagnose(EXPERIMENT, run([]), BASELINE)
        self.assertEqual(d["evidence_use"], "inconclusive")
        self.assertIsNone(history_outcome(d))

    def test_forced_kill_switch_test_is_not_experiment_evidence(self):
        r = run([sli("search", 1.0, 3000.0), {"kind": "kill_switch", "tripped": True, "forced_test": True},
                 {"kind": "recovery", "recovery_s": 12, "recovered": True}], status="aborted")
        d = diagnose(EXPERIMENT, r, BASELINE)
        self.check(d, r)
        self.assertEqual(d["evidence_use"], "kill_switch_test")
        self.assertIn("kill_switch_fired", d["flags"])
        self.assertIsNone(history_outcome(d))

    def test_slow_and_failed_recovery_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.yaml"
            policy_path.write_text(
                "abort_thresholds:\n  write_error_rate: 0.2\n  write_p95_multiplier: 3\n  freshness_p95_seconds: 30\n"
                "recovery:\n  timeout_seconds: 60\n",
                encoding="utf-8",
            )
            limits = load_thresholds(policy_path)

        slow = run(NORMAL[:3] + [{"kind": "recovery", "recovery_s": 95.0, "recovered": True}])
        d = diagnose(EXPERIMENT, slow, BASELINE, limits)
        self.assertIn("slow_recovery", d["flags"])
        self.assertEqual(history_outcome(d), "failure_observed")
        d = diagnose(EXPERIMENT, run(NORMAL[:3] + [{"kind": "recovery", "recovered": False}]), BASELINE, limits)
        self.assertIn("did_not_recover", d["flags"])
        self.assertEqual(history_outcome(d), "failure_observed")

    def test_non_forced_abort_counts_as_weak_history_but_remains_inconclusive(self):
        d = diagnose(EXPERIMENT, run(NORMAL, status="aborted", abort_reason="error_rate_above_configured_limit"), BASELINE)

        self.assertEqual(d["evidence_use"], "inconclusive")
        self.assertIn("non_forced_abort", d["flags"])
        self.assertEqual(history_outcome(d), "failure_observed")

    def test_wrong_experiment_and_missing_baseline_warn(self):
        d = diagnose(EXPERIMENT, run(NORMAL, experiment_id="other"), None)
        self.assertTrue(any("other" in w for w in d["warnings"]))
        self.assertTrue(any("no baseline" in w for w in d["warnings"]))

    def test_m4_normal_reconstruction_recovers_but_is_not_scored_without_samples(self):
        raw, reconstructed = runner_fixture("m4-normal-docs-reconstruction.json")
        diagnosis = diagnose(EXPERIMENT, raw, BASELINE)

        self.assertEqual(reconstructed["_source"], "reconstructed from docs/m4-results.md, not a raw record")
        self.assertEqual(diagnosis["recovery_status"], "recovered")
        self.assertEqual(diagnosis["recovery_seconds"], 4.2)
        self.assertEqual(diagnosis["evidence_use"], "inconclusive")
        self.assertIsNone(history_outcome(diagnosis))

    def test_m4_forced_reconstruction_is_excluded(self):
        raw, _ = runner_fixture("m4-forced-test-docs-reconstruction.json")
        diagnosis = diagnose(EXPERIMENT, raw, BASELINE)

        self.assertTrue(diagnosis["kill_switch"]["forced_test"])
        self.assertEqual(diagnosis["evidence_use"], "kill_switch_test")
        self.assertEqual(diagnosis["recovery_seconds"], 9.0)
        self.assertIsNone(history_outcome(diagnosis))

    def test_dry_run_marker_is_not_evidence(self):
        r, _ = runner_fixture("m4-normal-docs-reconstruction.json")
        r["dry_run"] = True
        d = diagnose(EXPERIMENT, r, BASELINE)
        self.assertEqual(d["evidence_use"], "dry_run")
        self.assertIsNone(history_outcome(d))

    def test_policy_is_the_only_diagnosis_threshold_source(self):
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.yaml"
            policy_path.write_text(
                "abort_thresholds:\n  write_error_rate: 0.27\n  write_p95_multiplier: 4\n  freshness_p95_seconds: 45\n"
                "recovery:\n  timeout_seconds: 60\n",
                encoding="utf-8",
            )
            thresholds = load_thresholds(policy_path)

        self.assertEqual(thresholds, {
            "fail_error_rate": 0.27,
            "write_p95_multiplier": 4.0,
            "freshness_p95_seconds": 45.0,
            "slow_recovery_s": 60.0,
        })
        self.assertEqual(classify(0.28, None, {"error_rate": 0.0}, thresholds), "fail")
        self.assertEqual(classify(None, 41.0, {"p95_ms": 10.0}, thresholds), "fail")
        self.assertEqual(classify_freshness(46.0, 40.0, thresholds["freshness_p95_seconds"]), "fail")


if __name__ == "__main__":
    unittest.main()
