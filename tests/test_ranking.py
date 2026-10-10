import unittest

from ai.ranking import measured_facts, rank_findings, render_markdown
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


if __name__ == "__main__":
    unittest.main()
