import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from app.main import create_app  # noqa: E402
from app.settings import Settings  # noqa: E402
from ai.report import generate_findings  # noqa: E402


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.metrics = root / "metrics"
        self.metrics.mkdir()
        self.settings = Settings(
            ROOT / "deploy" / "ditto" / "docker-compose.yml", (), "http://127.0.0.1:9090",
            root / "db.sqlite3", ROOT / "configs" / "experiment-policy.yaml", metrics_dir=self.metrics,
        )
        self.client = TestClient(create_app(self.settings))

    def tearDown(self):
        self.tmp.cleanup()

    def _sample(self, kind, seconds_ago, **fields):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")
        with (self.metrics / f"{kind}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": kind, "source": "live", "run_id": "r", "timestamp": ts, **fields}) + "\n")

    def test_planner_agent_status_follows_the_stored_baselines(self):
        def planner():
            return next(a for a in self.client.get("/api/v1/agents").json()["agents"] if a["id"] == "experiment_planning")["status"]
        self.assertEqual(planner(), "blocked_until_baseline")
        self.client.app.state.orchestrator.repository.save_baseline("graph-v1", {"status": "available", "write_samples": 200})
        self.assertEqual(planner(), "ready")

    def test_ranked_findings_endpoint_responds_and_ranks_a_stored_run(self):
        empty = self.client.get("/api/v1/findings/ranked")
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json(), {"ranked": []})
        run = {"run_id": "run-r1", "experiment_id": "e1", "status": "completed", "recovery_verified": True,
               "diagnosis": {"target_component": "things-search", "action": "pause_container"},
               "observations": [{"phase": "fault", "write_error_rate": 0.0, "search_error_rate": 1.0}]}
        orchestrator = self.client.app.state.orchestrator
        orchestrator.repository.save_run(run)
        orchestrator.repository.save_findings("run-r1", generate_findings(run))
        ranked = self.client.get("/api/v1/findings/ranked").json()["ranked"]
        self.assertEqual([item["run_id"] for item in ranked], ["run-r1"])
        self.assertEqual(ranked[0]["rank"], 1)

    def test_live_slis_and_series_come_from_metric_files(self):
        empty = self.client.get("/api/v1/slis").json()
        self.assertEqual(empty["write_samples"], 0)
        self.assertFalse(empty["live"])
        for ago in (20, 10, 2):
            self._sample("write", ago, success=True, latency_ms=20.0)
            self._sample("search", ago, success=ago != 10, latency_ms=30.0)
            self._sample("freshness", ago, success=True, freshness_seconds=0.02, method="sse")
        self._sample("write", 300, success=False, latency_ms=5.0)  # outside the 30 s window
        slis = self.client.get("/api/v1/slis?window=30").json()
        self.assertEqual(slis["write_samples"], 3)
        self.assertEqual(slis["write_error_rate"], 0.0)
        self.assertAlmostEqual(slis["search_error_rate"], 1 / 3)
        self.assertEqual(slis["freshness_method"], "sse")
        self.assertTrue(slis["live"])
        series = self.client.get("/api/v1/slis/series?minutes=1&bucket=10").json()
        self.assertEqual(len(series["points"]), 6)
        self.assertEqual(sum(point["write_samples"] for point in series["points"]), 3)

    def test_policy_dependency_map_watchdog_and_baselines_are_read_only_views(self):
        policy = self.client.get("/api/v1/policy").json()
        self.assertFalse(policy["api_executes"])
        self.assertIn("mongodb", policy["denied_targets"])
        self.assertEqual(policy["abort_thresholds"]["write_error_rate"], 0.2)
        graph = self.client.get("/api/v1/dependency-map").json()
        self.assertEqual(graph["status"], "available")
        self.assertIn("fleet-workload", {node["id"] for node in graph["nodes"]})
        self.assertIsInstance(self.client.get("/api/v1/watchdog").json()["events"], list)
        self.assertEqual(self.client.get("/api/v1/baselines").json()["baselines"], [])

    def test_dashboard_is_served_and_execute_stays_disabled(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        for view in ("view-overview", "view-architecture", "view-experiments", "view-runs", "view-safety", "approve-dialog"):
            self.assertIn(view, page.text)
        self.assertEqual(self.client.get("/assets/app.js").status_code, 200)
        # Dashboard files also resolve at the site root (matches the static hosting layout).
        for asset in ("/app.js", "/config.js", "/lab.css", "/lab.js"):
            with self.subTest(asset=asset):
                self.assertEqual(self.client.get(asset).status_code, 200)
        lab = self.client.get("/lab")
        self.assertEqual(lab.status_code, 200)
        self.assertIn('src="config.js"', lab.text)
        self.assertEqual(self.client.post("/api/v1/experiments/x/execute").status_code, 503)


if __name__ == "__main__":
    unittest.main()
