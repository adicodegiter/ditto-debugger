import json
import unittest
from pathlib import Path

from ai.report_export import derive_weaknesses
from ai.request_evidence import load_samples, request_facts
from ai.retest import compare_retest_runs

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence" / "windows-host"


def record(run_id):
    return json.loads((EVIDENCE / f"{run_id}.json").read_text(encoding="utf-8"))


def facts_for(run_id):
    return request_facts(record(run_id), load_samples(EVIDENCE / "samples" / f"{run_id}.jsonl"))


class RequestEvidenceTests(unittest.TestCase):
    def test_day1_restart_facts_match_the_m4_results(self):
        facts = facts_for("run-c8b30b07c9f3")
        self.assertEqual(facts["search_failure_errors"], ["ReadTimeout"])
        self.assertEqual(facts["search_failure_wait_seconds"], 10.0)
        self.assertEqual(facts["search_outage_seconds"], 20.6)  # docs/m4-results.md: "about 21 s"
        self.assertEqual(facts["last_search_failure_after_rollback_seconds"], 2.0)
        self.assertEqual(facts["write_failures"], 0)

    def test_missing_samples_give_no_facts(self):
        self.assertEqual(request_facts(record("run-c8b30b07c9f3"), []), {})
        self.assertEqual(request_facts({"run_id": "x"}, [{"kind": "search"}]), {})

    def test_burst_without_failures_reports_none_not_zero(self):
        facts = facts_for("run-4083aa7e13a1")
        self.assertEqual(facts["search_failures"], 0)
        self.assertIsNone(facts["search_failure_wait_seconds"])
        self.assertIsNone(facts["search_outage_seconds"])

    def test_improvement_needs_evidence_on_both_sides(self):
        def with_facts(run_id, facts):
            run = record(run_id)
            run["_reported_results"] = facts
            return run
        before, after = facts_for("run-c8b30b07c9f3"), facts_for("run-a09cb1bcbb7c")
        result = compare_retest_runs(with_facts("run-c8b30b07c9f3", before), with_facts("run-a09cb1bcbb7c", after))
        self.assertEqual(result["overall_improvement"], "client-side improvement demonstrated; server-side fail-fast not demonstrated")
        no_rate = {k: v for k, v in after.items() if k != "write_samples_per_second"}
        result = compare_retest_runs(with_facts("run-c8b30b07c9f3", before), with_facts("run-a09cb1bcbb7c", no_rate))
        self.assertEqual(result["overall_improvement"], "not demonstrated")

    def test_weakness_rules_follow_the_measurements(self):
        ids = ["run-c8b30b07c9f3", "run-370897363c60", "run-4083aa7e13a1", "run-a09cb1bcbb7c"]
        runs = [record(run_id) for run_id in ids]
        weaknesses = derive_weaknesses(runs, {}, {run_id: facts_for(run_id) for run_id in ids})
        self.assertEqual([w["severity"] for w in weaknesses], ["high", "medium", "low"])
        self.assertEqual(set(weaknesses[0]["run_ids"]), {"run-c8b30b07c9f3", "run-370897363c60", "run-a09cb1bcbb7c"})
        self.assertNotIn("run-4083aa7e13a1", weaknesses[0]["run_ids"])  # the burst had no failed search
        # Without request-level samples there is no evidence for the fail-fast weakness.
        self.assertFalse(any(w["title"].startswith("Search does not fail fast") for w in derive_weaknesses(runs, {}, {})))


if __name__ == "__main__":
    unittest.main()
