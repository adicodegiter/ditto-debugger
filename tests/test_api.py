from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.main import create_app
from app.settings import Settings


class LocalApiTests(unittest.TestCase):
    def test_health_and_dashboard_are_local_workbench_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                compose_file=root / "missing-compose.yml",
                kubernetes_manifests=(),
                prometheus_url="http://127.0.0.1:9090",
                database_path=root / "workbench.sqlite3",
                policy_path=ROOT / "configs" / "experiment-policy.yaml",
            )
            client = TestClient(create_app(settings))
            health = client.get("/api/v1/health").json()
            architecture = client.get("/api/v1/architecture").json()
            dashboard = client.get("/")
        self.assertFalse(health["execution_enabled"])
        self.assertEqual(architecture["status"], "unavailable")
        self.assertEqual(architecture["nodes"], [])
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("System overview", dashboard.text)

    def test_execute_endpoint_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root / "missing.yml", (), "http://127.0.0.1:9090", root / "db.sqlite3", ROOT / "configs" / "experiment-policy.yaml")
            response = TestClient(create_app(settings)).post("/api/v1/experiments/test/execute")
        self.assertEqual(response.status_code, 503)
        self.assertIn("disabled", response.json()["detail"])

    def test_autonomy_loop_is_advisory_and_never_executes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                compose_file=ROOT / "deploy" / "ditto" / "docker-compose.yml",
                kubernetes_manifests=(),
                prometheus_url="http://127.0.0.1:9090",
                database_path=root / "db.sqlite3",
                policy_path=ROOT / "configs" / "experiment-policy.yaml",
            )
            client = TestClient(create_app(settings))
            status = client.get("/api/v1/autonomy").json()
            # The loop is off by default and always reports that it does not execute.
            self.assertFalse(status["enabled"])
            self.assertFalse(status["executes"])
            # An on-demand advisory cycle runs discover -> measure -> plan and stays non-executing.
            cycle = client.post("/api/v1/autonomy/run").json()
            self.assertFalse(cycle["executes"])
            self.assertEqual(cycle["steps"]["discover"]["status"], "available")
            # Without Prometheus/live samples, planning is honestly blocked rather than faked.
            self.assertEqual(cycle["steps"]["plan"]["status"], "waiting_for_baseline")
            # The advisory cycle never persists a plan or an approval on its own.
            self.assertEqual(client.get("/api/v1/experiments?limit=20").json()["plans"], [])



if __name__ == "__main__":
    unittest.main()