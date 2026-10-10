"""M5 fault catalog: pause, CPU throttle, traffic burst — fixed recipes with exact rollbacks."""
import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from safety.docker_adapter import SubprocessDockerAdapter  # noqa: E402
from safety.policy import Policy, SafetyViolation  # noqa: E402
from safety.runner import ExperimentRunner  # noqa: E402
from safety.watchdog import run_watchdog  # noqa: E402
from tests.test_safety import HEALTHY, FakeClock, FakeDocker, FakeRecovery, FakeWatchdog, ScriptedObservations  # noqa: E402

POLICY = ROOT / "configs" / "experiment-policy.yaml"


def candidate(action, target, duration=10):
    return {"schema_version": "1.0", "id": f"{action}-{target}", "target_component": target, "action": action,
            "duration_seconds": duration, "hypothesis": "h", "predicted_impacts": ["x"], "risk_score": 0.2,
            "approval_required": True}


class Recorder:
    """Fake subprocess.run / Popen that records argv and returns canned output."""

    def __init__(self, states=None, inspect="0 0"):
        self.calls = []
        self.states = states or {}
        self.inspect = inspect
        self.popen_args = None

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        out = ""
        if argv[-3:-1] == ["ps", "-q"] or (len(argv) >= 3 and argv[-2:] == ["-q", argv[-1]] and "ps" in argv):
            out = "0123456789ab\n"
        elif "ps" in argv and "--format" in argv:
            out = "\n".join(json.dumps({"Service": name, "State": state}) for name, state in self.states.items())
        elif argv[:2] == ["docker", "inspect"]:
            out = self.inspect
        return subprocess.CompletedProcess(argv, 0, out, "")

    def popen(self, argv, **kwargs):
        self.popen_args = argv

        class Proc:
            terminated = False

            def poll(self):
                return None if not self.terminated else 0

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

        self.proc = Proc()
        return self.proc


def adapter(recorder):
    return SubprocessDockerAdapter("ditto", ROOT / "deploy" / "ditto" / "docker-compose.yml", None, run=recorder.run, popen=recorder.popen)


class FaultCatalogPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = Policy.from_file(POLICY)

    def test_mongodb_only_reachable_through_cpu_throttle(self):
        self.policy.validate_candidate(candidate("cpu_throttle", "mongodb"))
        for action in ("stop_container", "restart_container", "pause_container"):
            with self.subTest(action=action), self.assertRaises(SafetyViolation):
                self.policy.validate_candidate(candidate(action, "mongodb"))

    def test_traffic_burst_only_targets_the_api_entry_point(self):
        self.policy.validate_candidate(candidate("traffic_burst", "nginx"))
        with self.assertRaises(SafetyViolation):
            self.policy.validate_candidate(candidate("traffic_burst", "gateway"))
        with self.assertRaises(SafetyViolation):
            self.policy.validate_candidate(candidate("restart_container", "nginx"))

    def test_parameters_have_hard_bounds(self):
        raw = POLICY.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.yaml"
            path.write_text(raw.replace("cpus: 0.25", "cpus: 0"), encoding="utf-8")
            with self.assertRaises(SafetyViolation):
                Policy.from_file(path)
            path.write_text(raw.replace("rate_per_second: 10", "rate_per_second: 500"), encoding="utf-8")
            with self.assertRaises(SafetyViolation):
                Policy.from_file(path)
        self.assertEqual(self.policy.params_for("cpu_throttle"), {"cpus": 0.25})


class FaultRecipeTests(unittest.TestCase):
    def test_pause_and_unpause(self):
        recorder = Recorder(states={"things-search": "paused", "gateway": "running"})
        docker = adapter(recorder)
        docker.apply("pause_container", "things-search")
        docker.rollback("pause_container", "things-search")
        tails = [call[-2:] for call in recorder.calls]
        self.assertIn(["pause", "things-search"], tails)
        self.assertIn(["unpause", "things-search"], tails)
        self.assertEqual(tails[-1], ["start", "things-search"], "rollback ends with an idempotent start")

    def test_cpu_throttle_restores_unlimited_with_quota_minus_one(self):
        recorder = Recorder(inspect="0 0")
        docker = adapter(recorder)
        state = docker.capture("cpu_throttle", "mongodb")
        self.assertEqual(state, {"container_id": "0123456789ab", "cpu_quota": 0, "cpu_period": 0})
        docker.apply("cpu_throttle", "mongodb", {"cpus": 0.25}, state)
        docker.rollback("cpu_throttle", "mongodb", state)
        updates = [call for call in recorder.calls if call[:2] == ["docker", "update"]]
        self.assertEqual(updates[0], ["docker", "update", "--cpu-period", "100000", "--cpu-quota", "25000", "0123456789ab"])
        # `--cpus 0` leaves the limit in place (verified on Docker 24); quota -1 removes it.
        self.assertEqual(updates[1], ["docker", "update", "--cpu-quota", "-1", "0123456789ab"])

    def test_cpu_throttle_restores_an_existing_limit_exactly(self):
        recorder = Recorder(inspect="50000 100000")
        docker = adapter(recorder)
        state = docker.capture("cpu_throttle", "things-search")
        docker.rollback("cpu_throttle", "things-search", state)
        self.assertEqual(recorder.calls[-1], ["docker", "update", "--cpu-period", "100000", "--cpu-quota", "50000", "0123456789ab"])

    def test_traffic_burst_is_a_bounded_subprocess_terminated_on_rollback(self):
        recorder = Recorder()
        docker = adapter(recorder)
        docker.apply("traffic_burst", "nginx", {"duration_seconds": 30, "rate_per_second": 10, "vehicles": 16})
        argv = recorder.popen_args
        self.assertIn("fleet_workload.py", argv[1])
        self.assertEqual(argv[argv.index("--duration-seconds") + 1], "30", "the burst ends by itself")
        self.assertIn("metrics-burst", argv[argv.index("--metrics-dir") + 1], "burst samples never mix with SLI samples")
        docker.rollback("traffic_burst", "nginx")
        self.assertTrue(recorder.proc.terminated)


class FaultRunnerTests(unittest.TestCase):
    def test_runner_passes_params_and_captured_state_to_apply_rollback_and_watchdog(self):
        policy = Policy.from_file(POLICY)
        clock = FakeClock()
        docker, watchdog = FakeDocker(), FakeWatchdog()
        runner = ExperimentRunner(policy, docker, ScriptedObservations(HEALTHY), FakeRecovery(), watchdog,
                                  clock=clock.monotonic, wall_clock=clock.time, sleep=clock.sleep)
        item = candidate("cpu_throttle", "mongodb", duration=10)
        approval = {"decision": "approve", "candidate_hash": ExperimentRunner.candidate_hash(item),
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
        baseline = {"baseline_id": "b", "telemetry_status": "available", "captured_at": datetime.now(timezone.utc).isoformat(), "sli": {"write_p95_ms": 50.0}}
        run = runner.execute(item, approval, baseline)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(docker.last_params, {"cpus": 0.25, "duration_seconds": 10})
        self.assertEqual(docker.last_rollback[0], "cpu_throttle")
        self.assertEqual(docker.last_rollback[1]["cpu_quota"], 0)
        self.assertEqual(watchdog.armed_actions[0][0], "cpu_throttle")
        self.assertEqual(run["action_state"]["container_id"], "abc123abc123")


class FaultWatchdogTests(unittest.TestCase):
    def _args(self, log, **overrides):
        clock = FakeClock()
        values = dict(target="things-search", action="pause_container", deadline_epoch=clock.time() + 1, policy=POLICY,
                      compose_file=ROOT / "deploy" / "ditto" / "docker-compose.yml", env_file=None, log=log, attempts=1,
                      cpu_quota=None, cpu_period=None, container_id=None)
        values.update(overrides)
        return argparse.Namespace(**values), clock

    def test_watchdog_runs_the_action_specific_rollback(self):
        calls = []

        class Adapter:
            def rollback(self, action, target, state=None):
                calls.append((action, target, state))

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "w.jsonl"
            args, clock = self._args(log)
            self.assertEqual(run_watchdog(args, sleep=clock.sleep, clock=clock.time, adapter=Adapter()), 0)
            args, clock = self._args(log, target="mongodb", action="cpu_throttle", cpu_quota=0, cpu_period=0, container_id="0123456789ab")
            self.assertEqual(run_watchdog(args, sleep=clock.sleep, clock=clock.time, adapter=Adapter()), 0)
            args, clock = self._args(log, target="nginx", action="traffic_burst")
            self.assertEqual(run_watchdog(args, sleep=clock.sleep, clock=clock.time, adapter=Adapter()), 0)
            args, clock = self._args(log, target="mongodb", action="stop_container")
            self.assertEqual(run_watchdog(args, sleep=clock.sleep, clock=clock.time, adapter=Adapter()), 2)
            events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(calls[0][:2], ("pause_container", "things-search"))
        self.assertEqual(calls[1][2], {"cpu_quota": 0, "cpu_period": 0, "container_id": "0123456789ab"})
        self.assertEqual(len(calls), 2, "a burst is bounded by design; nothing to undo")
        self.assertIn("bounded_by_design", [e.get("result") for e in events])
        self.assertEqual(events[-1]["event"], "refused")


if __name__ == "__main__":
    unittest.main()
