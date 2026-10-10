from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agents.discovery import DittoDiscoveryAgent
from app.agents.mapping import ArchitectureMapperAgent
from app.agents.planning import ExperimentPlanningAgent
from app.store import LocalRepository

POLICY = ROOT / "configs" / "experiment-policy.yaml"
SCHEMA = json.loads((ROOT / "schemas" / "experiment.schema.json").read_text(encoding="utf-8"))


def real_graph():
    return ArchitectureMapperAgent().map(DittoDiscoveryAgent(ROOT / "deploy" / "ditto" / "docker-compose.yml").discover())


def evidence(graph):
    telemetry = {"status": "available", "coverage": 1.0, "observations": [{"metric": "up", "quality": "fresh"}]}
    baseline = {"baseline_id": "baseline-test", "captured_at": datetime.now(timezone.utc).isoformat(),
                "graph_version": graph["graph_version"], "telemetry_status": "available"}
    return telemetry, baseline


class P4PlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = LocalRepository(Path(self.tmp.name) / "t.sqlite3")
        self.graph = real_graph()
        self.telemetry, self.baseline = evidence(self.graph)

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, planner_config=None):
        return ExperimentPlanningAgent(POLICY, self.repo, planner_config).plan(self.graph, self.telemetry, self.baseline)

    def test_first_experiment_is_low_blast_radius_things_search_on_the_real_compose_graph(self):
        result = self.plan()
        limit = 0.35
        self.assertTrue(all(c["score_terms"]["impact"] <= limit for c in result["candidates"]))
        restart = next(c for c in result["candidates"] if c["target_component"] == "things-search" and c["action"] == "restart_container")
        self.assertEqual(restart["predicted_slis"], {"write": "hold", "search": "fail", "freshness": "hold"})
        self.assertIn("low-blast-radius first experiment", restart["reason"])
        deferred = {(r.get("action"), r["target"]) for r in result["policy_rejections"] if "deferred" in r["reason"]}
        self.assertIn(("restart_container", "gateway"), deferred)
        self.assertNotIn("mongodb", {item["target_component"] for item in result["candidates"]})
        for candidate in result["candidates"]:
            Draft202012Validator(SCHEMA).validate(candidate)

    def test_gateway_becomes_eligible_after_a_completed_run_and_is_ranked_first(self):
        self.repo.record_outcome("things-search", "restart_container", "failure_observed", "run:one")
        result = self.plan()
        self.assertEqual(result["candidates"][0]["target_component"], "gateway")
        self.assertEqual(result["candidates"][0]["predicted_slis"]["write"], "fail")
        for c in result["candidates"]:
            Draft202012Validator(SCHEMA).validate(c)

    def test_target_whose_effect_cannot_be_observed_is_rejected(self):
        cfg = Path(self.tmp.name) / "planner.yaml"
        cfg.write_text(yaml.safe_dump({"measured_slis": ["write"]}), encoding="utf-8")
        planner = ExperimentPlanningAgent(POLICY, self.repo, cfg)
        # config file replaces only the keys it sets; predicted_effects falls back to empty -> nothing observable
        result = planner.plan(self.graph, self.telemetry, self.baseline)
        self.assertEqual(result["candidates"], [])
        self.assertTrue(all("observable" in r["reason"] for r in result["policy_rejections"] if r["target"] in ("gateway", "things-search")))

    def test_only_write_measured_falls_back_to_gateway_and_says_so(self):
        cfg = Path(self.tmp.name) / "planner.yaml"
        cfg.write_text(yaml.safe_dump({"measured_slis": ["write"], "predicted_effects": {
            "gateway": {"write": "fail"}, "things-search": {"search": "fail"}}}), encoding="utf-8")
        result = ExperimentPlanningAgent(POLICY, self.repo, cfg).plan(self.graph, self.telemetry, self.baseline)
        self.assertEqual({c["target_component"] for c in result["candidates"]}, {"gateway"})
        self.assertIn("lowest impact available", result["candidates"][0]["reason"])

    def test_forbidden_targets_never_appear(self):
        self.repo.record_outcome("things-search", "restart_container", "no_failure_observed", "run:one")
        result = ExperimentPlanningAgent(POLICY, self.repo).plan(self.graph, self.telemetry, self.baseline, 10)
        pairs = {(c["action"], c["target_component"]) for c in result["candidates"]}
        names = {target for _, target in pairs}
        self.assertFalse(names & {"policies", "things", "connectivity", "ditto-ui"})
        # Explicit action targets may narrow scope but cannot override a denied target.
        self.assertFalse({target for _, target in pairs} & {"mongodb"})
        self.assertEqual({a for a, t in pairs if t == "nginx"} - {"traffic_burst"}, set())
        self.assertFalse(result["execution_enabled"])

    def test_ranking_is_reproducible(self):
        self.repo.record_outcome("things-search", "restart_container", "no_failure_observed", "run:one")
        self.assertEqual(json.dumps(self.plan(), sort_keys=True), json.dumps(self.plan(), sort_keys=True))

    def test_prediction_confirmation_does_not_lower_the_experiment_rank(self):
        initial = self.plan()["candidates"][0]
        self.repo.record_outcome("things-search", "restart_container", "prediction_confirmed", "run:confirmed")
        history = self.repo.history_for("things-search", "restart_container")
        after_confirmation = self.plan()["candidates"][0]

        self.assertEqual(history["prediction_confirmations"], 1)
        self.assertEqual(history["failures"], 0)
        self.assertEqual(history["trials"], 0)
        self.assertEqual(after_confirmation["selection_score"], initial["selection_score"])

    def test_policy_enabled_stop_candidate_is_clamped_and_uses_graph_impact_paths(self):
        policy_path = Path(self.tmp.name) / "policy.yaml"
        policy_path.write_text(yaml.safe_dump({
            "scope": {"allowed_targets": ["gateway", "things-search"], "denied_targets": []},
            "max_duration_seconds": 60,
            "baseline_max_age_seconds": 3600,
            "max_risk_score": 0.35,
            "target_risk_scores": {"gateway": 0.2, "things-search": 0.3},
            "actions": {"stop_container": {"enabled": True, "duration_seconds_default": 90, "max_duration_seconds": 60}},
        }), encoding="utf-8")
        graph = deepcopy(self.graph)
        result = ExperimentPlanningAgent(policy_path, self.repo, ROOT / "configs" / "planner.yaml").plan(
            graph, self.telemetry, self.baseline
        )

        self.assertTrue(result["candidates"])
        top = result["candidates"][0]
        self.assertEqual((top["action"], top["target_component"], top["duration_seconds"]),
                         ("stop_container", "things-search", 60))
        self.assertEqual(top["schema_version"], "1.0")
        Draft202012Validator(SCHEMA).validate(top)
        self.assertIn("1 direct graph dependent(s): gateway", top["reason"])
        self.assertIn("impact paths:", top["reason"])
        self.assertIn("ai/dependency_map.json", top["reason"])
        self.assertIn("ditto_docs:architecture-overview", top["reason"])
        self.assertNotIn("configured dependent", top["reason"])

    def test_default_policy_proposes_m4_things_search_stop_at_policy_default_duration(self):
        policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
        expected_duration = int(policy["actions"]["stop_container"]["duration_seconds_default"])

        result = self.plan()
        candidate = next(
            item for item in result["candidates"]
            if item["target_component"] == "things-search" and item["action"] == "stop_container"
        )

        self.assertEqual(candidate["duration_seconds"], expected_duration)
        Draft202012Validator(SCHEMA).validate(candidate)

    def test_empty_impact_paths_name_missing_dependents_and_source(self):
        graph = deepcopy(self.graph)
        graph["impact_paths"]["things-search"] = [
            {"impacted_component": "things-search", "path": ["things-search"], "edges": []},
        ]
        result = ExperimentPlanningAgent(POLICY, self.repo, ROOT / "configs" / "planner.yaml").plan(
            graph, self.telemetry, self.baseline
        )
        candidate = next(item for item in result["candidates"] if item["target_component"] == "things-search")

        self.assertIn("no direct dependents recorded in ai/dependency_map.json", candidate["reason"])
        self.assertIn("source: ai/dependency_map.json", candidate["reason"])

    def test_action_target_list_cannot_override_mongodb_deny(self):
        policy_path = Path(self.tmp.name) / "deny.yaml"
        policy_path.write_text(yaml.safe_dump({
            "scope": {"allowed_targets": ["things-search"], "denied_targets": ["mongodb"]},
            "max_duration_seconds": 60,
            "baseline_max_age_seconds": 3600,
            "max_risk_score": 0.35,
            "target_risk_scores": {"things-search": 0.3, "mongodb": 0.3},
            "actions": {"cpu_throttle": {
                "enabled": True,
                "targets": ["things-search", "mongodb"],
                "duration_seconds_default": 60,
            }},
        }), encoding="utf-8")
        result = ExperimentPlanningAgent(policy_path, self.repo, ROOT / "configs" / "planner.yaml").plan(
            self.graph, self.telemetry, self.baseline
        )

        self.assertTrue(result["candidates"])
        self.assertTrue(all(item["target_component"] != "mongodb" for item in result["candidates"]))

    def test_only_exact_policy_targets_and_enabled_faults_are_proposed(self):
        policy_path = Path(self.tmp.name) / "policy.yaml"
        policy_path.write_text(yaml.safe_dump({
            "scope": {"allowed_targets": ["things-search"], "denied_targets": ["things-search-alias"]},
            "max_duration_seconds": 60,
            "baseline_max_age_seconds": 3600,
            "max_risk_score": 0.35,
            "target_risk_scores": {"things-search": 0.3},
            "actions": {
                "stop_container": {"enabled": True, "duration_seconds_default": 60},
                "restart_container": {"enabled": False, "duration_seconds_default": 10},
            },
        }), encoding="utf-8")

        result = ExperimentPlanningAgent(policy_path, self.repo, ROOT / "configs" / "planner.yaml").plan(
            self.graph, self.telemetry, self.baseline
        )

        self.assertTrue(all(item["target_component"] == "things-search" for item in result["candidates"]))
        self.assertEqual({item["action"] for item in result["candidates"]}, {"stop_container"})


if __name__ == "__main__":
    unittest.main()
