from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.main import create_app
from app.orchestrator import AgentOrchestrator
from app.settings import Settings
from app.store import LocalRepository
from safety.docker_adapter import DockerCommandError, SubprocessDockerAdapter, parse_ps_json
from safety.kill_switch import KillSwitch
from safety.observations import SliWindowObservationProvider, unrelated_services_healthy
from safety.policy import AbortThresholds, Policy, SafetyViolation
from safety.recovery import HttpRecoveryChecker
from safety.runner import ExperimentRunner, RunnerError
from safety.scoring import calculate_resilience_score
from safety.watchdog import run_watchdog

HEALTHY = {
    "telemetry_available": True,
    "unrelated_component_healthy": True,
    "write_samples": 20,
    "write_error_rate": 0.0,
    "write_p95_ms": 50.0,
    "freshness_samples": 20,
    "freshness_p95_seconds": 1.0,
}


class FakeClock:
    """Monotonic + wall clock that only advance when the runner sleeps."""

    def __init__(self) -> None:
        self.mono = 1000.0
        self.wall = datetime.now(timezone.utc).timestamp()

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds


class FakeDocker:
    def __init__(self, fail_start: bool = False, fail_preflight: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_start = fail_start
        self.fail_preflight = fail_preflight

    def preflight(self, target: str) -> None:
        self.calls.append(("preflight", target))
        if self.fail_preflight:
            raise DockerCommandError("no configuration file provided")

    def capture(self, action: str, target: str) -> dict:
        return {"cpu_quota": 0, "cpu_period": 0, "container_id": "abc123abc123"} if action == "cpu_throttle" else {}

    def apply(self, action: str, target: str, params=None, state=None) -> None:
        self.calls.append((action, target))
        self.last_params = params

    def rollback(self, action: str, target: str, state=None) -> None:
        # Container actions roll back with an idempotent start (as the real recipes do).
        self.calls.append(("start", target))
        self.last_rollback = (action, state)
        if self.fail_start:
            raise DockerCommandError("start failed")


class ScriptedObservations:
    """Returns the scripted observations in order (last one repeats)."""

    def __init__(self, *values: dict[str, object]) -> None:
        self.values = list(values)
        self.calls: list[float | None] = []

    def observe(self, target: str, since: float | None = None) -> dict[str, object]:
        self.calls.append(since)
        return dict(self.values.pop(0) if len(self.values) > 1 else self.values[0])


class FakeRecovery:
    def __init__(self, healthy_after_calls: int = 1) -> None:
        self.calls = 0
        self.healthy_after_calls = healthy_after_calls

    def verify(self, target: str) -> dict[str, object]:
        self.calls += 1
        ok = self.calls >= self.healthy_after_calls
        return {"healthy": ok, "canary": ok}


class FailingRecovery:
    def verify(self, target: str) -> dict[str, object]:
        raise RuntimeError("probe failed")


class FakeWatchdog:
    def __init__(self) -> None:
        self.armed: list[tuple[str, float]] = []
        self.disarmed = 0

    def arm(self, target: str, deadline_epoch: float, action: str = "restart_container", state=None) -> str:
        self.armed.append((target, deadline_epoch))
        self.armed_actions = getattr(self, "armed_actions", []) + [(action, state)]
        return "handle"

    def disarm(self, handle: object) -> None:
        self.disarmed += 1


def candidate(target: str = "things-search", action: str = "stop_container", duration: int = 10) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "id": f"{action}-{target}-test",
        "target_component": target,
        "action": action,
        "duration_seconds": duration,
        "hypothesis": "Search degrades and recovers; writes are unaffected.",
        "predicted_impacts": ["search_path"],
        "risk_score": 0.3,
        "approval_required": True,
    }


class SafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = Policy.from_file(ROOT / "configs" / "experiment-policy.yaml")
        self.baseline = {
            "baseline_id": "baseline-test",
            "telemetry_status": "available",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "sli": {"write_p95_ms": 50.0},
        }
        self.clock = FakeClock()

    def approval_for(self, item: dict[str, object], expired: bool = False) -> dict[str, str]:
        expiry = datetime.now(timezone.utc) + timedelta(minutes=-1 if expired else 5)
        return {"decision": "approve", "candidate_hash": ExperimentRunner.candidate_hash(item), "expires_at": expiry.isoformat()}

    def runner(self, docker, observations, recovery=None, watchdog=None, policy=None) -> ExperimentRunner:
        return ExperimentRunner(
            policy or self.policy, docker, observations, recovery or FakeRecovery(), watchdog or FakeWatchdog(),
            clock=self.clock.monotonic, wall_clock=self.clock.time, sleep=self.clock.sleep,
        )

    # --- policy ---------------------------------------------------------------
    def test_forbidden_target_and_action_are_rejected(self):
        with self.assertRaises(SafetyViolation):
            self.policy.validate_candidate(candidate("mongodb"))
        with self.assertRaises(SafetyViolation):
            self.policy.validate_candidate(candidate(action="run_shell"))

    def test_policy_rejects_unsupported_actions_and_disarmed_thresholds(self):
        raw = (ROOT / "configs" / "experiment-policy.yaml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.yaml"
            path.write_text(raw.replace("  stop_container:", "  run_shell:"), encoding="utf-8")
            with self.assertRaises(SafetyViolation):
                Policy.from_file(path)
        with self.assertRaises(SafetyViolation):
            AbortThresholds.from_mapping({"write_error_rate": 1.5})
        with self.assertRaises(SafetyViolation):
            AbortThresholds.from_mapping({"window_seconds": 0})

    def test_policy_matches_claude_md_thresholds(self):
        thresholds = self.policy.thresholds
        self.assertEqual((thresholds.window_seconds, thresholds.write_error_rate, thresholds.write_p95_multiplier, thresholds.freshness_p95_seconds), (30, 0.20, 3.0, 30))
        self.assertEqual(self.policy.max_duration_seconds, 60)
        self.policy.validate_candidate(candidate(duration=60))

    # --- kill switch ------------------------------------------------------------
    def test_missing_observation_fields_abort_closed(self):
        switch = KillSwitch(self.baseline, self.policy)
        self.assertEqual(switch.evaluate({}, 0).reason, "missing_samples")
        for field in HEALTHY:
            partial = {key: value for key, value in HEALTHY.items() if key != field}
            with self.subTest(missing=field):
                self.assertTrue(switch.evaluate(partial, 0).aborted)

    def test_kill_switch_limits_cannot_be_overridden_by_observation(self):
        switch = KillSwitch(self.baseline, self.policy)
        decision = switch.evaluate({**HEALTHY, "write_error_rate": 0.5, "error_rate_limit": 1.0}, 0)
        self.assertEqual(decision.reason, "error_rate_above_configured_limit")

    def test_kill_switch_thresholds(self):
        switch = KillSwitch(self.baseline, self.policy)
        self.assertFalse(switch.evaluate(HEALTHY, 0).aborted)
        self.assertEqual(switch.evaluate({**HEALTHY, "write_p95_ms": 151.0}, 0).reason, "p95_latency_above_baseline_multiplier")
        self.assertEqual(switch.evaluate({**HEALTHY, "freshness_p95_seconds": 31.0}, 0).reason, "freshness_p95_above_limit")
        self.assertEqual(switch.evaluate({**HEALTHY, "unrelated_component_healthy": False}, 0).reason, "unrelated_component_unhealthy")
        self.assertEqual(switch.evaluate({**HEALTHY, "write_samples": 1}, 0).reason, "missing_samples")
        self.assertEqual(switch.evaluate({**HEALTHY, "write_p95_ms": float("nan")}, 0).reason, "missing_samples")
        self.assertEqual(switch.evaluate(HEALTHY, 61).reason, "experiment_deadline_exceeded")
        # Every write failed: no latency exists, but the reason must be the error rate.
        self.assertEqual(switch.evaluate({**HEALTHY, "write_error_rate": 1.0, "write_p95_ms": None}, 0).reason, "error_rate_above_configured_limit")
        self.assertEqual(KillSwitch({}, self.policy).evaluate(HEALTHY, 0).reason, "baseline_unhealthy")

    # --- runner -------------------------------------------------------------------
    def test_kill_switch_aborts_mid_fault_and_rolls_back_immediately(self):
        docker, watchdog = FakeDocker(), FakeWatchdog()
        bad = {**HEALTHY, "write_error_rate": 0.5}
        observations = ScriptedObservations(HEALTHY, HEALTHY, bad, HEALTHY)
        item = candidate(duration=60)
        run = self.runner(docker, observations, watchdog=watchdog).execute(item, self.approval_for(item), self.baseline)
        self.assertEqual(run["status"], "aborted")
        self.assertEqual(run["abort_reason"], "error_rate_above_configured_limit")
        injected = datetime.fromisoformat(run["timeline"]["injected_at"])
        rolled_back = datetime.fromisoformat(run["timeline"]["rollback_at"])
        self.assertLessEqual((rolled_back - injected).total_seconds(), 2 * self.policy.thresholds.poll_interval_seconds + 0.01)
        self.assertEqual(docker.calls, [("preflight", "things-search"), ("stop_container", "things-search"), ("start", "things-search")])
        self.assertEqual(run["cleanup_state"], "complete")
        self.assertTrue(run["recovery_verified"], "aborted runs must still be recovery-checked")
        self.assertEqual(watchdog.disarmed, 1)

    def test_runner_completes_only_after_recovery_and_times_from_rollback(self):
        docker = FakeDocker()
        recovery = FakeRecovery(healthy_after_calls=3)
        item = candidate(duration=10)
        run = self.runner(docker, ScriptedObservations(HEALTHY), recovery).execute(item, self.approval_for(item), self.baseline)
        self.assertEqual(run["status"], "completed")
        self.assertTrue(run["recovery_verified"])
        # Two failed checks x 3 s poll interval: recovery time counts from rollback, not from injection.
        self.assertAlmostEqual(run["recovery_time_seconds"], 2 * self.policy.recovery_poll_interval_seconds, places=3)
        self.assertGreater(len(run["observations"]), 3, "the kill switch must be polled during the fault")

    def test_recovery_requires_slis_back_within_thresholds(self):
        degraded = {**HEALTHY, "write_p95_ms": 500.0}
        # pre-fault, 5 fault polls (10 s / 2 s), then recovery polls stay degraded until timeout.
        observations = ScriptedObservations(HEALTHY, HEALTHY, HEALTHY, HEALTHY, HEALTHY, HEALTHY, degraded)
        item = candidate(duration=10)
        run = self.runner(FakeDocker(), observations).execute(item, self.approval_for(item), self.baseline)
        self.assertFalse(run["recovery_verified"])
        self.assertEqual(run["status"], "failed")
        self.assertFalse(run["recovery"]["slis_within_baseline"])

    def test_steady_state_failure_refuses_before_any_fault(self):
        docker, watchdog = FakeDocker(), FakeWatchdog()
        item = candidate()
        run = self.runner(docker, ScriptedObservations({**HEALTHY, "write_samples": 0}), watchdog=watchdog).execute(item, self.approval_for(item), self.baseline)
        self.assertEqual(run["status"], "aborted")
        self.assertTrue(run["abort_reason"].startswith("steady_state_not_met"))
        self.assertEqual(docker.calls, [("preflight", "things-search")])
        self.assertEqual(watchdog.armed, [])

    def test_watchdog_is_armed_before_injection_and_left_armed_if_cleanup_fails(self):
        docker, watchdog = FakeDocker(fail_start=True), FakeWatchdog()
        item = candidate(duration=10)
        armed_from = self.clock.wall
        run = self.runner(docker, ScriptedObservations(HEALTHY), watchdog=watchdog).execute(item, self.approval_for(item), self.baseline)
        self.assertEqual(len(watchdog.armed), 1)
        target, deadline = watchdog.armed[0]
        self.assertEqual(target, "things-search")
        self.assertAlmostEqual(deadline, armed_from + 10 + self.policy.watchdog_grace_seconds, places=3)
        self.assertEqual(run["cleanup_state"], "failed")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(watchdog.disarmed, 0, "watchdog must stay armed when cleanup fails")

    def test_preflight_failure_never_injects(self):
        docker = FakeDocker(fail_preflight=True)
        item = candidate()
        run = self.runner(docker, ScriptedObservations(HEALTHY)).execute(item, self.approval_for(item), self.baseline)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["cleanup_state"], "not_started")
        self.assertEqual(docker.calls, [("preflight", "things-search")])

    def test_pre_fault_window_supplies_baseline_p95_when_stored_baseline_has_none(self):
        baseline = {key: value for key, value in self.baseline.items() if key != "sli"}
        item = candidate(duration=4)
        run = self.runner(FakeDocker(), ScriptedObservations(HEALTHY)).execute(item, self.approval_for(item), baseline)
        self.assertEqual(run["baseline_write_p95_ms"], 50.0)
        self.assertEqual(run["status"], "completed")

    def test_expired_approval_stale_or_missing_baseline_block_before_docker(self):
        docker = FakeDocker()
        runner = self.runner(docker, ScriptedObservations(HEALTHY))
        with self.assertRaises(SafetyViolation):
            runner.execute(candidate(), self.approval_for(candidate(), expired=True), self.baseline)
        with self.assertRaises(SafetyViolation):
            runner.execute(candidate(), self.approval_for(candidate()), {"telemetry_status": "unavailable"})
        stale = {**self.baseline, "captured_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}
        with self.assertRaises(SafetyViolation):
            runner.execute(candidate(), self.approval_for(candidate()), stale)
        self.assertEqual(docker.calls, [])

    def test_naive_approval_timestamp_is_rejected_cleanly(self):
        approval = self.approval_for(candidate())
        approval["expires_at"] = "2999-01-01T00:00:00"
        with self.assertRaises(SafetyViolation):
            self.runner(FakeDocker(), ScriptedObservations(HEALTHY)).execute(candidate(), approval, self.baseline)

    def test_recovery_probe_failure_returns_failed_run_after_cleanup(self):
        item = candidate(duration=4)
        run = self.runner(FakeDocker(), ScriptedObservations(HEALTHY), FailingRecovery()).execute(item, self.approval_for(item), self.baseline)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["cleanup_state"], "complete")
        self.assertFalse(run["recovery_verified"])

    def test_approval_cannot_be_replayed(self):
        item = candidate(duration=2)
        approval = self.approval_for(item)
        runner = self.runner(FakeDocker(), ScriptedObservations(HEALTHY))
        runner.execute(item, approval, self.baseline)
        with self.assertRaises(SafetyViolation):
            runner.execute(item, approval, self.baseline)

    # --- adapters -----------------------------------------------------------------
    def test_docker_adapter_always_passes_compose_file_and_fixed_commands(self):
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if "config" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps({"name": "ditto", "services": {"things-search": {}}}), "")
            if "ps" in command:
                return subprocess.CompletedProcess(command, 0, '{"Service":"things-search","State":"running","Health":"healthy"}\n', "")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            compose = Path(directory) / "docker-compose.yml"
            compose.write_text("services: {}", encoding="utf-8")
            adapter = SubprocessDockerAdapter("ditto", compose, Path("pins.env"), run=fake_run)
            adapter.preflight("things-search")
            adapter.apply("stop_container", "things-search")
            adapter.start("things-search")
            with self.assertRaises(DockerCommandError):
                adapter.apply("run_shell", "things-search")
        for command in calls:
            self.assertEqual(command[:4], ["docker", "compose", "-p", "ditto"])
            self.assertIn("-f", command)
            self.assertIn("--env-file", command)
        self.assertEqual([command[-2:] for command in calls[-2:]], [["stop", "things-search"], ["start", "things-search"]])

    def test_ps_json_parsing_and_unrelated_health(self):
        states = parse_ps_json('{"Service":"gateway","State":"running","Health":"healthy"}\n{"Service":"things-search","State":"exited","Health":""}\n')
        self.assertTrue(unrelated_services_healthy(states, "things-search"))
        self.assertFalse(unrelated_services_healthy(states, "gateway"))
        self.assertFalse(unrelated_services_healthy({}, "gateway"))

    def test_sli_observation_provider_reads_real_window_and_never_invents_data(self):
        now = datetime.now(timezone.utc)

        class States:
            def service_states(self):
                return {"gateway": {"state": "running", "health": "healthy"}}

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            provider = SliWindowObservationProvider(base, States(), 30, clock=lambda: now.timestamp())
            empty = provider.observe("things-search")
            self.assertFalse(empty["telemetry_available"])
            self.assertTrue(KillSwitch(self.baseline, self.policy).evaluate(empty, 0).aborted)
            old = (now - timedelta(seconds=120)).isoformat()
            recent = (now - timedelta(seconds=5)).isoformat()
            lines = [
                {"kind": "write", "success": True, "latency_ms": 40, "timestamp": recent},
                {"sli": "write", "ok": False, "latency_ms": 900, "ts": recent},
                {"kind": "write", "success": False, "latency_ms": 10, "timestamp": old},
                {"kind": "freshness", "freshness_seconds": 2.0, "timestamp": recent},
            ]
            (base / "write.jsonl").write_text("\n".join(json.dumps(line) for line in lines[:3]) + "\nnot json\n", encoding="utf-8")
            (base / "freshness.jsonl").write_text(json.dumps(lines[3]) + "\n", encoding="utf-8")
            observed = provider.observe("things-search")
        self.assertEqual(observed["write_samples"], 2, "samples outside the 30 s window are ignored")
        self.assertEqual(observed["write_error_rate"], 0.5)
        self.assertEqual(observed["write_p95_ms"], 40.0, "latency p95 uses successful writes only")
        self.assertEqual(observed["freshness_method"], "poll-proxy")
        self.assertTrue(observed["telemetry_available"])

    def test_recovery_checker_uses_routed_health_and_authenticated_canary(self):
        seen: list[tuple[str, str | None]] = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(request, timeout):
            seen.append((request.full_url, request.get_header("Authorization")))
            if request.get_header("Authorization") is None and "/api/" in request.full_url:
                raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)
            return Response()

        result = HttpRecoveryChecker("http://127.0.0.1:8080", "ditto", "ditto", opener=opener).verify("things-search")
        self.assertTrue(result["healthy"] and result["canary"])
        self.assertEqual(seen[0], ("http://127.0.0.1:8080/health", None))
        self.assertTrue(seen[1][0].startswith("http://127.0.0.1:8080/api/2/search/things"))
        self.assertTrue(seen[1][1].startswith("Basic "))
        with self.assertRaises(ValueError):
            HttpRecoveryChecker("http://example.com", "ditto", "ditto")

    def test_watchdog_fires_cleanup_at_deadline_and_refuses_unlisted_targets(self):
        started: list[str] = []

        class Adapter:
            def rollback(self, action, target, state=None):
                started.append(target)

        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "watchdog.jsonl"
            args = argparse.Namespace(
                target="things-search", deadline_epoch=clock.time() + 5, policy=ROOT / "configs" / "experiment-policy.yaml",
                compose_file=ROOT / "deploy" / "ditto" / "docker-compose.yml", env_file=None, log=log, attempts=3,
            )
            self.assertEqual(run_watchdog(args, sleep=clock.sleep, clock=clock.time, adapter=Adapter()), 0)
            self.assertEqual(started, ["things-search"])
            self.assertGreaterEqual(clock.time(), args.deadline_epoch)
            refused = argparse.Namespace(**{**vars(args), "target": "mongodb"})
            self.assertEqual(run_watchdog(refused, sleep=clock.sleep, clock=clock.time, adapter=Adapter()), 2)
            events = [json.loads(line)["event"] for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events, ["armed", "fired_cleanup", "refused"])

    # --- process boundary ---------------------------------------------------------
    def test_api_never_executes_even_when_execution_is_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root / "missing.yml", (), "http://127.0.0.1:9090", root / "db.sqlite3", ROOT / "configs" / "experiment-policy.yaml", execution_enabled=True)
            response = TestClient(create_app(settings)).post("/api/v1/experiments/anything/execute")
        self.assertEqual(response.status_code, 503)
        self.assertIn("operator CLI", response.json()["detail"])
        source = (ROOT / "backend" / "app" / "orchestrator.py").read_text(encoding="utf-8")
        self.assertNotIn("SubprocessDockerAdapter", source)
        self.assertNotIn("subprocess", source)

    def test_prepared_candidate_matches_the_approved_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root / "missing.yml", (), "http://127.0.0.1:9090", root / "db.sqlite3", ROOT / "configs" / "experiment-policy.yaml")
            orchestrator = AgentOrchestrator(settings)
            repository = orchestrator.repository
            baseline = repository.save_baseline("graph-1", {"status": "available"})
            item = {**candidate(), "evidence_refs": [f"baseline:{baseline['baseline_id']}"]}
            repository.save_plan({"plan_id": "plan-1", "candidates": [item]})
            stored = repository.get_experiment(item["id"])
            orchestrator.record_approval(item["id"], "approve", "test", stored["candidate_hash"])
            prepared, approval, _ = orchestrator.prepare_execution(item["id"])
        self.assertEqual(ExperimentRunner.candidate_hash(prepared), approval["candidate_hash"])
        self.assertNotIn("approval", prepared)

    # --- M4 forced kill-switch test ------------------------------------------------------
    def test_forced_test_trips_the_real_kill_switch_then_recovers_with_normal_thresholds(self):
        docker, watchdog = FakeDocker(), FakeWatchdog()
        item = {**candidate(action="restart_container", duration=10), "predicted_slis": {"write": "hold", "search": "fail"}}
        # Healthy, normal-looking samples throughout: only the lowered fault-phase limit can trip.
        run = self.runner(docker, ScriptedObservations(HEALTHY), watchdog=watchdog).execute(
            item, self.approval_for(item), self.baseline, forced_test=True
        )
        self.assertTrue(run["forced_test"])
        self.assertTrue(run["forced_test_passed"])
        self.assertEqual(run["status"], "aborted")
        self.assertEqual(run["abort_reason"], "p95_latency_above_baseline_multiplier")
        self.assertTrue(run["recovery_verified"], "recovery must use the normal thresholds")
        self.assertEqual(docker.calls[-1], ("start", "things-search"))
        self.assertEqual(watchdog.disarmed, 1)
        self.assertEqual(run["resilience_score"]["status"], "not_scored_forced_test")
        self.assertEqual(run["prediction"]["predicted_slis"], {"write": "hold", "search": "fail"})

    def test_forced_test_only_passes_when_the_lowered_limit_fires(self):
        # Samples disappear during the fault: the run aborts, but for missing samples,
        # which does not prove the threshold path.
        missing = {**HEALTHY, "write_samples": 0}
        item = candidate(action="restart_container", duration=10)
        run = self.runner(FakeDocker(), ScriptedObservations(HEALTHY, missing, HEALTHY)).execute(
            item, self.approval_for(item), self.baseline, forced_test=True
        )
        self.assertEqual(run["status"], "aborted")
        self.assertEqual(run["abort_reason"], "missing_samples")
        self.assertFalse(run["forced_test_passed"])

    def test_cli_approval_cannot_be_scripted(self):
        import io
        from unittest import mock
        from safety import cli

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {"DATABASE_PATH": str(root / "db.sqlite3"), "DITTO_COMPOSE_FILE": str(root / "missing.yml")}
            with mock.patch.dict("os.environ", env):
                orchestrator = cli._orchestrator()
                baseline = orchestrator.repository.save_baseline("graph-1", {"status": "available"})
                item = {**candidate(), "evidence_refs": [f"baseline:{baseline['baseline_id']}"]}
                orchestrator.repository.save_plan({"plan_id": "plan-cli", "candidates": [item]})
                with self.assertRaises(SystemExit):
                    cli.main(["approve", item["id"], "--by", "script", "--reason", "x", "--yes"])
                piped = io.StringIO("APPROVE\n")
                with mock.patch("sys.stdin", piped), mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                    code = cli.main(["approve", item["id"], "--by", "script", "--reason", "x"])
                stored = cli._orchestrator().get_experiment(item["id"])
        self.assertEqual(code, 2)
        self.assertEqual(stored["approval_status"], "pending", "a piped APPROVE must not record an approval")

    def test_failed_action_command_still_rolls_back(self):
        class HalfFailingDocker(FakeDocker):
            def apply(self, action, target):
                super().apply(action, target)
                raise DockerCommandError("restart failed after stopping the container")

        docker = HalfFailingDocker()
        item = candidate(action="restart_container", duration=4)
        run = self.runner(docker, ScriptedObservations(HEALTHY)).execute(item, self.approval_for(item), self.baseline)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(docker.calls[-1], ("start", "things-search"), "cleanup must run even if the action command failed")
        self.assertEqual(run["cleanup_state"], "complete")
        self.assertLessEqual(run["timeline"]["injected_at"], run["timeline"]["rollback_at"])

    def test_normal_run_is_not_marked_as_forced_test(self):
        item = candidate(duration=4)
        run = self.runner(FakeDocker(), ScriptedObservations(HEALTHY)).execute(item, self.approval_for(item), self.baseline)
        self.assertNotIn("forced_test", run)
        self.assertEqual(run["status"], "completed")

    # --- integration with P3 baseline capture and findings ----------------------------
    def test_kill_switch_reads_write_p95_from_baseline_captured_from_sli_metrics(self):
        from monitor.sli import append_jsonl
        from monitor.window import baseline_from_metrics

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # One live run spanning 130 s (baselines need >= 120 s of one run).
            start = datetime.now(timezone.utc) - timedelta(seconds=135)
            for index, latency in enumerate((10, 20, 30, 40)):
                ts = (start + timedelta(seconds=index * 130 / 3)).isoformat().replace("+00:00", "Z")
                common = {"source": "live", "run_id": "run-baseline", "timestamp": ts}
                append_jsonl(root / "write.jsonl", {"kind": "write", "success": True, "latency_ms": latency, **common})
                append_jsonl(root / "search.jsonl", {"kind": "search", "success": True, "latency_ms": latency, **common})
                append_jsonl(root / "freshness.jsonl", {"kind": "freshness", "freshness_seconds": 1.0, **common})
            repository = LocalRepository(root / "db.sqlite3")
            stored = repository.save_baseline("graph-1", baseline_from_metrics(root))
            baseline = repository.get_baseline(stored["baseline_id"])
        switch = KillSwitch(baseline, self.policy)
        self.assertEqual(switch.evaluate({**HEALTHY, "write_p95_ms": 40.0}, 0).aborted, False)
        self.assertEqual(switch.evaluate({**HEALTHY, "write_p95_ms": 121.0}, 0).reason, "p95_latency_above_baseline_multiplier")

    def test_operator_runs_store_deterministic_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root / "missing.yml", (), "http://127.0.0.1:9090", root / "db.sqlite3", ROOT / "configs" / "experiment-policy.yaml")
            orchestrator = AgentOrchestrator(settings)
            run = {"run_id": "run-x", "observations": [{**HEALTHY, "error_rate": 0.6}], "abort_reason": "error_rate_above_configured_limit", "recovery_verified": True}
            findings = orchestrator.record_operator_run(run)
            stored = orchestrator.list_findings("run-x")
        self.assertTrue(findings)
        self.assertEqual(len(stored), len(findings))

    # --- scoring / audit (unchanged behaviour) --------------------------------------
    def test_score_is_suppressed_for_incomplete_or_unsafe_runs(self):
        incomplete = calculate_resilience_score({"score_components": {"availability": 1.0}, "baseline_valid": True})
        unsafe = calculate_resilience_score({"safety_violation": True, "score_components": {"availability": 1.0}})
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertIsNone(incomplete["score"])
        self.assertEqual(unsafe["status"], "unsafe")

    def test_non_finite_risk_and_score_values_are_rejected(self):
        unsafe = candidate()
        unsafe["risk_score"] = float("nan")
        with self.assertRaises(SafetyViolation):
            self.policy.validate_candidate(unsafe)
        score = calculate_resilience_score({
            "score_components": {"availability": float("nan"), "latency": 1.0, "recovery": 1.0, "containment": 1.0, "detection": 1.0},
            "baseline_valid": True,
            "recovery_verified": True,
        })
        self.assertNotEqual(score["status"], "complete")

    def test_audit_events_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = LocalRepository(Path(directory) / "audit.sqlite3")
            repository.record_audit("safety_preflight_started", {"run_id": "run-test"})
            events = repository.list_audit()
        self.assertEqual(events[0]["event"], "safety_preflight_started")
        self.assertEqual(events[0]["payload"]["run_id"], "run-test")


if __name__ == "__main__":
    unittest.main()
