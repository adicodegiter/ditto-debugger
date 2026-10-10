from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from ai.diagnosis import diagnose, history_outcome
from ai.run_record import normalize_run_record
from backend.app.agents.planning import ExperimentPlanningAgent

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_METADATA = {"_fixture", "_source", "_reported_results"}


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def runner_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in FIXTURE_METADATA}


class ReconstructedRunFixtureTests(unittest.TestCase):
    def test_normal_m4_fixture_keeps_reported_bounds_and_unknown_record_fields(self):
        source = fixture("m4-normal-docs-reconstruction.json")
        raw = runner_payload(source)
        reported = source["_reported_results"]
        plan_context = {"target_component": reported["target"], "action": reported["action"]}

        normalized = normalize_run_record(raw, experiment=plan_context)
        diagnosis = diagnose(plan_context, normalized)

        self.assertTrue(source["_fixture"])
        self.assertEqual(source["_source"], "reconstructed from docs/m4-results.md, not a raw record")
        self.assertEqual(normalized["run_id"], "run-c8b30b07c9f3")
        self.assertEqual(normalized["target"], "things-search")
        self.assertEqual(normalized["fault_type"], "restart_container")
        self.assertEqual(normalized["planned_duration_s"], 10.0)
        self.assertIsNone(normalized["write_error_rate"])
        self.assertIsNone(normalized["write_p95_ms"])
        self.assertIsNone(normalized["search_outage_seconds"])
        self.assertEqual(normalized["recovery_state"], "recovered")
        self.assertEqual(normalized["recovery_time_seconds"], 4.2)
        self.assertEqual(reported["kill_switch_polls_without_breach"], 5)
        self.assertAlmostEqual(reported["search_outage_seconds_approx"], 21.0)
        self.assertEqual(reported["write_error_rate"], 0.0)
        self.assertEqual(reported["write_p95_ms_upper_bound"], 19.6)
        self.assertEqual(reported["write_p95_limit_ms"], 47)
        self.assertEqual(diagnosis["recovery_status"], "recovered")
        self.assertEqual(diagnosis["recovery_seconds"], 4.2)
        self.assertEqual(diagnosis["evidence_use"], "inconclusive")
        self.assertIsNone(history_outcome(diagnosis))
        self.assertFalse(ExperimentPlanningAgent.should_count_history_record(normalized))

    def test_forced_m4_fixture_is_identified_and_never_counted(self):
        source = fixture("m4-forced-test-docs-reconstruction.json")
        raw = runner_payload(source)
        reported = source["_reported_results"]
        plan_context = {"target_component": reported["target"], "action": reported["action"]}

        normalized = normalize_run_record(raw, experiment=plan_context)
        diagnosis = diagnose(plan_context, normalized)

        self.assertTrue(source["_fixture"])
        self.assertEqual(normalized["run_id"], "run-d3f744a96f7a")
        self.assertTrue(normalized["forced_test"])
        self.assertTrue(normalized["aborted"])
        self.assertEqual(normalized["abort_reason"], "p95_latency_above_baseline_multiplier")
        self.assertEqual(normalized["planned_duration_s"], 10.0)
        self.assertEqual(normalized["recovery_state"], "recovered")
        self.assertEqual(normalized["recovery_time_seconds"], 9.0)
        self.assertEqual(reported["fault_elapsed_seconds_approx"], 2.5)
        self.assertEqual(reported["rollback_seconds_approx"], 0.5)
        self.assertEqual(reported["write_p95_ms"], 21.2)
        self.assertEqual(reported["forced_write_p95_limit_ms"], 0.17)
        self.assertEqual(reported["score_status"], "not_scored_forced_test")
        self.assertEqual(diagnosis["evidence_use"], "kill_switch_test")
        self.assertEqual(diagnosis["recovery_status"], "recovered")
        self.assertIsNone(history_outcome(diagnosis))
        self.assertFalse(ExperimentPlanningAgent.should_count_history_record(normalized))

    def test_missing_recovery_marker_stays_inconclusive(self):
        raw = runner_payload(fixture("m4-normal-docs-reconstruction.json"))
        raw.pop("recovery_verified")
        raw.pop("recovery_time_seconds")
        normalized = normalize_run_record(raw)

        self.assertEqual(normalized["recovery_state"], "unknown")
        self.assertIsNone(normalized["recovery_time_seconds"])

    def test_recovery_duration_without_verification_does_not_prove_recovery(self):
        raw = runner_payload(fixture("m4-normal-docs-reconstruction.json"))
        raw.pop("recovery_verified")
        normalized = normalize_run_record(raw)

        self.assertEqual(normalized["recovery_state"], "unknown")
        self.assertEqual(normalized["recovery_time_seconds"], 4.2)

    def test_explicit_failed_verification_is_distinct_from_missing_verification(self):
        raw = runner_payload(fixture("m4-normal-docs-reconstruction.json"))
        raw["recovery_verified"] = False
        normalized = normalize_run_record(raw)

        self.assertEqual(normalized["recovery_state"], "not_recovered")
        self.assertEqual(normalized["recovery_time_seconds"], 4.2)

    def test_dry_run_marker_variant_is_not_experiment_evidence_or_history(self):
        raw = runner_payload(fixture("m4-normal-docs-reconstruction.json"))
        raw["dry_run"] = True
        normalized = normalize_run_record(raw)
        diagnosis = diagnose({}, normalized)

        self.assertTrue(normalized["dry_run"])
        self.assertEqual(diagnosis["evidence_use"], "dry_run")
        self.assertIsNone(history_outcome(diagnosis))
        self.assertFalse(ExperimentPlanningAgent.should_count_history_record(normalized))

    def test_unmarked_abort_variant_is_inconclusive_but_counts_as_weak_history(self):
        raw = runner_payload(fixture("m4-normal-docs-reconstruction.json"))
        raw["status"] = "aborted"
        normalized = normalize_run_record(raw)
        diagnosis = diagnose({}, normalized)

        self.assertFalse(normalized["forced_test"])
        self.assertTrue(normalized["aborted"])
        self.assertEqual(diagnosis["evidence_use"], "inconclusive")
        self.assertEqual(history_outcome(diagnosis), "failure_observed")
        self.assertTrue(ExperimentPlanningAgent.should_count_history_record(normalized))

    def test_incomplete_documentation_fixture_never_counts_as_real_history(self):
        normalized = normalize_run_record(runner_payload(fixture("m4-normal-docs-reconstruction.json")))

        self.assertEqual(normalized["recovery_state"], "recovered")
        self.assertFalse(normalized["sli_windows"])
        self.assertFalse(ExperimentPlanningAgent.should_count_history_record(normalized))

    def test_dry_run_note_has_no_fabricated_run_identity(self):
        source = fixture("m4-dry-run-docs-note.json")
        raw = runner_payload(source)

        self.assertTrue(source["_fixture"])
        self.assertTrue(raw["dry_run"])
        self.assertTrue(source["_reported_results"]["rehearsals_passed"])
        self.assertNotIn("run_id", raw)
        with self.assertRaisesRegex(ValueError, "run_id"):
            normalize_run_record(raw)

    def test_adapter_requires_a_record_id_for_diagnosis(self):
        raw = runner_payload(fixture("m4-normal-docs-reconstruction.json"))
        raw.pop("run_id")
        with self.assertRaisesRegex(ValueError, "run_id"):
            normalize_run_record(raw)


if __name__ == "__main__":
    unittest.main()
