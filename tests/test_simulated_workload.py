import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from app.main import create_app  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.workbench_views import sli_window  # noqa: E402
from monitor.window import baseline_from_metrics  # noqa: E402
from simulator.simulated_workload import backfill_simulated_samples  # noqa: E402


class SimulatedWorkloadTests(unittest.TestCase):
    """WORKLOAD_MODE=simulated: labelled synthetic samples drive baseline -> plan end to end."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.metrics = self.root / "metrics"

    def tearDown(self):
        self.tmp.cleanup()

    def _settings(self, workload_mode: str) -> Settings:
        return Settings(
            ROOT / "deploy" / "ditto" / "docker-compose.yml", (), "http://127.0.0.1:9090",
            self.root / "db.sqlite3", ROOT / "configs" / "experiment-policy.yaml",
            metrics_dir=self.metrics, workload_mode=workload_mode,
        )

    def test_simulated_samples_are_invisible_to_the_default_baseline_path(self):
        backfill_simulated_samples(self.metrics, duration_seconds=130, vehicles=2)
        baseline = baseline_from_metrics(self.metrics)
        self.assertEqual(baseline["status"], "unavailable")
        self.assertIsNone(baseline["run_id"])
        self.assertIn("No live workload samples found", baseline["warnings"][0])

    def test_simulated_baseline_is_available_and_stamped_when_allowed(self):
        backfill_simulated_samples(self.metrics, duration_seconds=130, vehicles=2)
        baseline = baseline_from_metrics(self.metrics, allow_simulated=True)
        self.assertEqual(baseline["status"], "available")
        self.assertEqual(baseline["telemetry"]["status"], "healthy")
        self.assertTrue(str(baseline["run_id"]).startswith("sim-"))
        self.assertEqual(baseline["sample_source"], "simulated")
        self.assertIn("SIMULATED", baseline["note"])

    def test_sli_window_labels_simulated_samples_and_never_calls_them_live(self):
        backfill_simulated_samples(self.metrics, duration_seconds=10, vehicles=2)
        view = sli_window(self.metrics, 30)
        self.assertTrue(view["simulated"])
        self.assertFalse(view["live"])
        self.assertGreater(view["write_samples"], 0)

    def test_api_baseline_and_plan_work_end_to_end_in_simulated_mode(self):
        client = TestClient(create_app(self._settings("simulated")))
        backfill_simulated_samples(self.metrics, duration_seconds=130, vehicles=2)
        captured = client.post("/api/v1/baselines")
        self.assertEqual(captured.status_code, 200, captured.text)
        payload = captured.json()
        self.assertEqual(payload["telemetry_status"], "available")
        self.assertEqual(payload["telemetry"]["sample_source"], "simulated")
        baseline_id = payload["baseline_id"]
        stored = client.get("/api/v1/baselines?limit=1").json()["baselines"]
        self.assertEqual(stored[0]["baseline_id"], baseline_id)
        plan = client.post("/api/v1/experiments/plan", json={"baseline_id": baseline_id, "max_candidates": 3})
        self.assertEqual(plan.status_code, 201, plan.text)
        self.assertTrue(plan.json()["candidates"])

    def test_api_baseline_refuses_simulated_samples_in_live_mode(self):
        client = TestClient(create_app(self._settings("live")))
        backfill_simulated_samples(self.metrics, duration_seconds=130, vehicles=2)
        response = client.post("/api/v1/baselines")
        self.assertEqual(response.status_code, 503)
        self.assertIn("No live workload samples", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
