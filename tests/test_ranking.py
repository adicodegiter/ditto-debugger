import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from ai.ranking import load_run_files, measured_facts, rank_findings, render_markdown
from ai.report import generate_findings


def run(run_id, **extra):
    base = {"run_id": run_id, "status": "completed", "recovery_verified": True, "recovery_time_seconds": 4.2,
            "diagnosis": {"target_component": "things-search", "action": "restart_container"},
            "observations": [{"phase": "pre_fault", "write_error_rate": 0.0, "search_error_rate": 0.0},
                             {"phase": "fault", "write_error_rate": 0.0, "write_p95_ms": 19.6, "search_error_rate": 1.0}]}
    base.update(extra)
    return base


class RankingTests(unittest.TestCase):
    def test_real_search_outage_is_not_reported_as_absorbed(self):
        findings = generate_findings(run("r1"))
        self.assertNotIn("r1-absorbed", [f["id"] for f in findings])
        self.assertIn("r1-availability", [f["id"] for f in findings])

    def test_errors_outside_the_fault_phase_do_not_create_a_degradation_finding(self):
        quiet = run("q", observations=[{"phase": "pre_fault", "write_error_rate": 0.1, "search_error_rate": 0.0},
                                       {"phase": "fault", "write_error_rate": 0.0, "search_error_rate": 0.0},
                                       {"phase": "recovered", "write_error_rate": 0.2, "search_error_rate": 0.0}])
        self.assertEqual([f["id"] for f in generate_findings(quiet)], ["q-absorbed"])

    def test_ranks_by_severity_then_confidence_and_numbers_from_run(self):
        runs = [run("r1"), run("r2", recovery_verified=False)]
        findings = generate_findings(runs[0]) + generate_findings(runs[1])
        ranked = rank_findings(runs, findings)
        self.assertEqual([item["rank"] for item in ranked], list(range(1, len(ranked) + 1)))
        self.assertEqual(ranked[0]["severity"], "critical")
        severities = [{"critical": 4, "high": 3, "medium": 2, "info": 0}[item["severity"]] for item in ranked]
        self.assertEqual(severities, sorted(severities, reverse=True))
        search = next(f for f in measured_facts(runs[0]) if "search error rate" in f["fact"])
        self.assertEqual(search["value"], 1.0)
        self.assertTrue(all(f["source"].startswith("run:") for item in ranked for f in item["measured_facts"]))
        self.assertTrue(all(item["validation_test"] and item["hypothesis"]["text"] for item in ranked))

    def test_dry_runs_forced_tests_and_unknown_runs_are_not_ranked(self):
        runs = [run("d", dry_run=True), run("f", forced_test=True)]
        findings = generate_findings(runs[0]) + generate_findings(runs[1]) + [{**generate_findings(run("x"))[0]}]
        self.assertEqual(rank_findings(runs, findings), [])

    def test_missing_facts_are_omitted_not_invented(self):
        self.assertEqual(measured_facts({"run_id": "r", "observations": []}), [])

    def test_markdown_renders_and_handles_empty(self):
        self.assertIn("No scored runs", render_markdown([]))
        ranked = rank_findings([run("r1")], generate_findings(run("r1")))
        text = render_markdown(ranked)
        self.assertIn("Measured facts", text)
        self.assertIn("Validation test", text)

EVIDENCE = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "mac-local"


class SharedRunRecordTests(unittest.TestCase):
    def test_load_run_files_explains_missing_and_invalid_records(self):
        with self.assertRaises(SystemExit) as missing:
            load_run_files(["/nonexistent/run.json"])
        self.assertIn("git-ignored", str(missing.exception))
        with tempfile.TemporaryDirectory() as tmp:
            bad, norun = Path(tmp, "bad.json"), Path(tmp, "norun.json")
            bad.write_text("not json")
            norun.write_text("{}")
            for path in (bad, norun):
                with self.assertRaises(SystemExit):
                    load_run_files([str(path)])

    def test_shared_mac_records_are_real_runs_and_rank_with_sourced_facts(self):
        files = sorted(EVIDENCE.glob("run-*.json"))
        self.assertEqual(len(files), 2)
        runs = load_run_files([str(f) for f in files])
        for record in runs:
            self.assertFalse(record.get("dry_run") or record.get("forced_test"))
            self.assertIn(record["status"], {"completed", "aborted"})
        ranked = rank_findings(runs, [f for r in runs for f in generate_findings(r)])
        self.assertEqual({item["run_id"] for item in ranked}, {r["run_id"] for r in runs})
        self.assertTrue(all(fact["source"].startswith("run:") for item in ranked for fact in item["measured_facts"]))
        aborted = next(r for r in runs if r["status"] == "aborted")
        self.assertEqual(aborted["abort_reason"], "p95_latency_above_baseline_multiplier")
        self.assertTrue(any(f["fact"] == "kill switch abort reason" for item in ranked if item["run_id"] == aborted["run_id"] for f in item["measured_facts"]))

    def test_windows_records_rank_with_target_and_action_even_without_diagnosis(self):
        files = sorted((EVIDENCE.parent / "windows-host").glob("run-*.json"))
        self.assertEqual(len(files), 4)
        runs = load_run_files([str(f) for f in files])
        ranked = rank_findings(runs, [f for r in runs for f in generate_findings(r)])
        labels = {item["run_id"]: (item["action"], item["target"]) for item in ranked}
        # The M4 record pre-dates the diagnosis block: action from its planner id, target from its recovery check.
        self.assertEqual(labels["run-c8b30b07c9f3"], ("restart_container", "things-search"))
        self.assertNotIn(("unknown", "unknown"), labels.values())

    def test_unlabelled_record_stays_unknown_and_prefixes_match_the_planner(self):
        from ai.ranking import _ID_PREFIX_ACTIONS, _target_and_action
        from app.agents.planning import ACTION_PREFIX

        self.assertEqual(_ID_PREFIX_ACTIONS, {prefix: action for action, prefix in ACTION_PREFIX.items()})
        self.assertEqual(_target_and_action({"run_id": "r", "experiment_id": "mystery-1"}), ("unknown", "unknown"))


if __name__ == "__main__":
    unittest.main()
