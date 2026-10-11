"""Read-only dashboard views for the stretch goals: combined-run interaction and re-run-after-fix verdicts.

Uses the committed live records in docs/evidence/mac-local/stretch-goals/. Nothing here runs Docker.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from app.main import create_app  # noqa: E402
from app.settings import Settings  # noqa: E402

EVIDENCE = ROOT / "docs" / "evidence" / "mac-local" / "stretch-goals"
COMPOUND, BURST, PAUSE = "run-8f06b01db01a", "run-e075dab30e40", "run-a564ac28867c"
FIX_BEFORE, FIX_AFTER = "run-68b41cfba584", "run-07544c127e3d"


class StretchViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "metrics").mkdir()
        settings = Settings(ROOT / "deploy" / "ditto" / "docker-compose.yml", (), "http://127.0.0.1:9090",
                            root / "db.sqlite3", ROOT / "configs" / "experiment-policy.yaml", metrics_dir=root / "metrics")
        self.app = create_app(settings)
        self.client = TestClient(self.app)
        self.repo = self.app.state.orchestrator.repository

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, *run_ids, **overrides):
        for run_id in run_ids:
            path = next(EVIDENCE.glob(f"*/{run_id}.json"))
            run = {**json.loads(path.read_text(encoding="utf-8")), **overrides.get(run_id, {})}
            self.repo.save_run(run)

    def test_interaction_is_recomputed_not_the_stale_stored_verdict(self):
        self.store(COMPOUND, BURST, PAUSE)
        data = self.client.get(f"/api/v1/runs/{COMPOUND}/interaction").json()
        self.assertEqual(data["stored_verdict"], "inconclusive")  # saved before the solo runs were matched
        self.assertEqual(data["verdict"], "no_interaction")
        self.assertEqual(sorted(data["solo_run_ids"]), sorted([BURST, PAUSE]))
        self.assertEqual([(f["action"], f["start_offset_seconds"]) for f in data["faults"]], [("traffic_burst", 0), ("pause_container", 10)])

    def test_interaction_ignores_solo_runs_with_a_different_fix_live_or_recorded_later(self):
        self.store(COMPOUND, BURST, PAUSE, FIX_AFTER)
        data = self.client.get(f"/api/v1/runs/{COMPOUND}/interaction").json()
        self.assertNotIn(FIX_AFTER, data["solo_run_ids"])  # search-fail-fast was live: not a fair solo run
        self.store(PAUSE, **{PAUSE: {"created_at": "2099-01-01T00:00:00+00:00"}})  # now recorded after the combined run
        self.assertEqual(self.client.get(f"/api/v1/runs/{COMPOUND}/interaction").json()["verdict"], "inconclusive")

    def test_interaction_endpoint_for_single_runs_and_unknown_runs(self):
        self.store(PAUSE)
        self.assertEqual(self.client.get(f"/api/v1/runs/{PAUSE}/interaction").json()["status"], "not_compound")
        self.assertEqual(self.client.get("/api/v1/runs/nope/interaction").status_code, 404)

    def test_remediations_pair_the_fix_runs_and_verify_them_without_docker(self):
        self.store(FIX_BEFORE, FIX_AFTER)
        with mock.patch("subprocess.run", side_effect=AssertionError("the API must never run a process")):
            data = self.client.get("/api/v1/remediations").json()
        self.assertFalse(data["api_reads_docker"])
        fix = next(r for r in data["remediations"] if r["name"] == "search-fail-fast")
        self.assertEqual((fix["pair"]["before_run_id"], fix["pair"]["after_run_id"]), (FIX_BEFORE, FIX_AFTER))
        self.assertEqual(fix["pair"]["verdict"], "verified")
        self.assertEqual((fix["pair"]["before"]["search_failure_wait_seconds"], fix["pair"]["after"]["search_failure_wait_seconds"]), (3.0, 1.5))
        self.assertIs(fix["live_in_last_recorded_run"], True)
        client = next(r for r in data["remediations"] if r["name"] == "search-client-timeout")
        self.assertEqual(client["pair"]["verdict"], "verified")  # from the committed Windows evidence

    def test_no_pair_without_an_after_run(self):
        self.store(FIX_BEFORE)
        fix = next(r for r in self.client.get("/api/v1/remediations").json()["remediations"] if r["name"] == "search-fail-fast")
        self.assertIsNone(fix["pair"])
        self.assertIs(fix["live_in_last_recorded_run"], False)

    def test_lab_page_is_served(self):
        response = self.client.get("/lab")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Attack simulator", response.text)


if __name__ == "__main__":
    unittest.main()
