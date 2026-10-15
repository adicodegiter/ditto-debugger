import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from app.main import create_app  # noqa: E402
from app.seeding import seed_recorded_runs  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.store import LocalRepository  # noqa: E402


class SeededRunsTests(unittest.TestCase):
    """SEED_RECORDED_RUNS=true: committed evidence runs make the Runs page show real records."""

    def _client(self, seed: bool) -> TestClient:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        settings = Settings(
            ROOT / "deploy" / "ditto" / "docker-compose.yml", (), "http://127.0.0.1:9090",
            root / "db.sqlite3", ROOT / "configs" / "experiment-policy.yaml", metrics_dir=root / "metrics",
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SEED_RECORDED_RUNS", None)
            if seed:
                os.environ["SEED_RECORDED_RUNS"] = "true"
            return TestClient(create_app(settings))

    def test_seeded_runs_appear_labelled_and_exclude_dry_and_forced_tests(self):
        runs = self._client(seed=True).get("/api/v1/runs?limit=50").json()["runs"]
        self.assertTrue(runs)
        for run in runs:
            with self.subTest(run_id=run["run_id"]):
                self.assertTrue(str(run.get("seeded_evidence", "")).startswith("docs/evidence/"))
                self.assertFalse(run.get("dry_run"))
                self.assertFalse(run.get("forced_test"))
        ids = {run["run_id"] for run in runs}
        self.assertNotIn("run-d3f744a96f7a", ids)  # the forced kill-switch test is never seeded

    def test_seeded_run_is_fetchable_for_replay(self):
        client = self._client(seed=True)
        run_id = client.get("/api/v1/runs?limit=1").json()["runs"][0]["run_id"]
        response = client.get(f"/api/v1/runs/{run_id}")
        self.assertEqual(response.status_code, 200)
        run = response.json()
        self.assertIn("timeline", run)
        self.assertIn("observations", run)

    def test_seeding_is_idempotent_and_skips_a_store_that_already_has_runs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repository = LocalRepository(Path(tmp.name) / "db.sqlite3")
        first = seed_recorded_runs(repository, ROOT)
        self.assertGreater(first["seeded"], 0)
        second = seed_recorded_runs(repository, ROOT)
        self.assertEqual(second["seeded"], 0)
        self.assertEqual(second["reason"], "store_not_empty")

    def test_no_seeding_by_default(self):
        self.assertEqual(self._client(seed=False).get("/api/v1/runs").json()["runs"], [])


if __name__ == "__main__":
    unittest.main()
