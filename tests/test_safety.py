from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.store import LocalRepository
from safety.policy import Policy, SafetyViolation
from safety.runner import ExperimentRunner, RunnerError
from safety.scoring import calculate_resilience_score
from safety.kill_switch import KillSwitch


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def preflight(self, project: str, target: str) -> None:
        self.calls.append(("preflight", target))

    def stop(self, project: str, target: str) -> None:
        self.calls.append(("stop", target))

    def start(self, project: str, target: str) -> None:
        self.calls.append(("start", target))


class FakeObservations:
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value

    def observe(self) -> dict[str, object]:
        return self.value


class FakeRecovery:
    def verify(self, target: str) -> dict[str, object]:
        return {"healthy": True, "canary": True}


class FailingRecovery:
    def verify(self, target: str) -> dict[str, object]:
        raise RuntimeError("probe failed")


def candidate(target: str = "gateway") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "id": "restart-gateway-test",
        "target_component": target,
        "action": "restart_container",
        "duration_seconds": 1,
        "hypothesis": "The gateway recovers after a short restart.",
        "predicted_impacts": ["external_http_websocket_api"],
        "risk_score": 0.2,
        "approval_required": True,
    }


class SafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = Policy.from_file(ROOT / "configs" / "experiment-policy.yaml")
        self.baseline = {"baseline_id": "baseline-test", "telemetry_status": "available"}

    def approval_for(self, item: dict[str, object], expired: bool = False) -> dict[str, str]:
        expiry = datetime.now(timezone.utc) + timedelta(minutes=-1 if expired else 5)
        return {
            "decision": "approve",
            "candidate_hash": ExperimentRunner.candidate_hash(item),
            "expires_at": expiry.isoformat(),
        }

    def test_forbidden_target_and_action_are_rejected(self):
        with self.assertRaises(SafetyViolation):
            self.policy.validate_candidate(candidate("mongodb"))
        unsafe = candidate()
        unsafe["action"] = "run_shell"
        with self.assertRaises(SafetyViolation):
            self.policy.validate_candidate(unsafe)

    def test_runner_aborts_and_starts_target_again(self):
        docker = FakeDocker()
        runner = ExperimentRunner(
            self.policy,
            docker,
            FakeObservations({"telemetry_available": True, "baseline_healthy": True, "unrelated_component_healthy": True, "error_rate": 0.5}),
            FakeRecovery(),
            sleep=lambda _seconds: None,
        )
        run = runner.execute(candidate(), self.approval_for(candidate()), self.baseline)
        self.assertEqual(run["status"], "aborted")
        self.assertEqual(run["abort_reason"], "error_rate_above_configured_limit")
        self.assertEqual(run["cleanup_state"], "complete")
        self.assertEqual(docker.calls[-1], ("start", "gateway"))

    def test_runner_completes_only_after_recovery(self):
        docker = FakeDocker()
        runner = ExperimentRunner(
            self.policy,
            docker,
            FakeObservations({"telemetry_available": True, "baseline_healthy": True, "unrelated_component_healthy": True, "error_rate": 0.0}),
            FakeRecovery(),
            sleep=lambda _seconds: None,
        )
        run = runner.execute(candidate(), self.approval_for(candidate()), self.baseline)
        self.assertEqual(run["status"], "completed")
        self.assertTrue(run["recovery_verified"])

    def test_expired_approval_and_missing_baseline_block_before_docker(self):
        docker = FakeDocker()
        runner = ExperimentRunner(self.policy, docker, FakeObservations({}), FakeRecovery())
        with self.assertRaises(SafetyViolation):
            runner.execute(candidate(), self.approval_for(candidate(), expired=True), self.baseline)
        with self.assertRaises(SafetyViolation):
            runner.execute(candidate(), self.approval_for(candidate()), {"telemetry_status": "unavailable"})
        self.assertEqual(docker.calls, [])

    def test_score_is_suppressed_for_incomplete_or_unsafe_runs(self):
        incomplete = calculate_resilience_score({"score_components": {"availability": 1.0}, "baseline_valid": True})
        unsafe = calculate_resilience_score({"safety_violation": True, "score_components": {"availability": 1.0}})
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertIsNone(incomplete["score"])
        self.assertEqual(unsafe["status"], "unsafe")

    def test_audit_events_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = LocalRepository(Path(directory) / "audit.sqlite3")
            repository.record_audit("safety_preflight_passed", {"run_id": "run-test"})
            events = repository.list_audit()
        self.assertEqual(events[0]["event"], "safety_preflight_passed")
        self.assertEqual(events[0]["payload"]["run_id"], "run-test")

    def test_missing_observation_fields_abort_closed(self):
        decision = KillSwitch(self.baseline, self.policy).evaluate({}, 0)
        self.assertTrue(decision.aborted)
        self.assertEqual(decision.reason, "missing_samples")

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

    def test_naive_approval_timestamp_is_rejected_cleanly(self):
        docker = FakeDocker()
        approval = self.approval_for(candidate())
        approval["expires_at"] = "2999-01-01T00:00:00"
        runner = ExperimentRunner(self.policy, docker, FakeObservations({}), FakeRecovery())
        with self.assertRaises(SafetyViolation):
            runner.execute(candidate(), approval, self.baseline)

    def test_recovery_probe_failure_returns_failed_run_after_cleanup(self):
        docker = FakeDocker()
        runner = ExperimentRunner(
            self.policy,
            docker,
            FakeObservations({"telemetry_available": True, "baseline_healthy": True, "unrelated_component_healthy": True, "error_rate": 0.0}),
            FailingRecovery(),
            sleep=lambda _seconds: None,
        )
        run = runner.execute(candidate(), self.approval_for(candidate()), self.baseline)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["cleanup_state"], "complete")

    def test_approval_cannot_be_replayed(self):
        docker = FakeDocker()
        item = candidate()
        approval = self.approval_for(item)
        runner = ExperimentRunner(
            self.policy,
            docker,
            FakeObservations({"telemetry_available": True, "baseline_healthy": True, "unrelated_component_healthy": True, "error_rate": 0.0}),
            FakeRecovery(),
            sleep=lambda _seconds: None,
        )
        runner.execute(item, approval, self.baseline)
        with self.assertRaises(SafetyViolation):
            runner.execute(item, approval, self.baseline)


if __name__ == "__main__":
    unittest.main()