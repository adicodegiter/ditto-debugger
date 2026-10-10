from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai.diagnosis import diagnose, history_outcome

DIAG_SCHEMA = json.loads((ROOT / "schemas" / "diagnosis.schema.json").read_text(encoding="utf-8"))
RUN_SCHEMA = json.loads((ROOT / "schemas" / "experiment-run.schema.json").read_text(encoding="utf-8"))
BASELINE = {"slis": {"write": {"error_rate": 0.0, "p95_ms": 35.0}, "search": {"error_rate": 0.0, "p95_ms": 60.0},
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
        self.assertEqual(history_outcome(d), "failure_observed")

    def test_worse_than_predicted_is_flagged(self):
        r = run([sli("write", 0.5, 900.0), sli("search", 1.0, 3000.0), sli("freshness", 0.0, 40.0),
                 {"kind": "recovery", "recovery_s": 38.0, "recovered": True}])
        d = diagnose(EXPERIMENT, r, BASELINE)
        self.assertEqual(d["comparison"]["write"]["verdict"], "worse_than_predicted")
        self.assertIn("worse_than_predicted", d["flags"])

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
        d = diagnose(EXPERIMENT, run(NORMAL[:3] + [{"kind": "recovery", "recovery_s": 95.0, "recovered": True}]), BASELINE)
        self.assertIn("slow_recovery", d["flags"])
        d = diagnose(EXPERIMENT, run(NORMAL[:3] + [{"kind": "recovery", "recovered": False}]), BASELINE)
        self.assertIn("did_not_recover", d["flags"])

    def test_wrong_experiment_and_missing_baseline_warn(self):
        d = diagnose(EXPERIMENT, run(NORMAL, experiment_id="other"), None)
        self.assertTrue(any("other" in w for w in d["warnings"]))
        self.assertTrue(any("no baseline" in w for w in d["warnings"]))


if __name__ == "__main__":
    unittest.main()
