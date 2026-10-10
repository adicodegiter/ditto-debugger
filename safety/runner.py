from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .kill_switch import KillSwitch
from .policy import Policy, SafetyViolation
from .scoring import calculate_resilience_score


class RunnerError(RuntimeError):
    """Raised when a local experiment cannot be executed safely."""


class DockerAdapter(Protocol):
    def preflight(self, project: str, target: str) -> None: ...
    def stop(self, project: str, target: str) -> None: ...
    def start(self, project: str, target: str) -> None: ...


class ObservationProvider(Protocol):
    def observe(self) -> dict[str, Any]: ...


class RecoveryChecker(Protocol):
    def verify(self, target: str) -> dict[str, Any]: ...


class SubprocessDockerAdapter:
    """Executes only fixed Docker Compose subcommands for the configured project."""

    def __init__(self, executable: str = "docker") -> None:
        self.executable = executable

    def _run(self, project: str, command: str, target: str) -> None:
        subprocess.run(
            [self.executable, "compose", "-p", project, command, target],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def preflight(self, project: str, target: str) -> None:
        result = subprocess.run(
            [self.executable, "compose", "-p", project, "config", "--services"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        services = set(result.stdout.split())
        if target not in services:
            raise RunnerError("The target is not present in the pinned Compose project.")

    def stop(self, project: str, target: str) -> None:
        self._run(project, "stop", target)

    def start(self, project: str, target: str) -> None:
        self._run(project, "start", target)


class ExperimentRunner:
    def __init__(
        self,
        policy: Policy,
        docker: DockerAdapter,
        observations: ObservationProvider,
        recovery: RecoveryChecker,
        audit: Callable[[str, dict[str, Any]], None] | None = None,
        consume_approval: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.policy = policy
        self.docker = docker
        self.observations = observations
        self.recovery = recovery
        self.audit = audit or (lambda _event, _payload: None)
        self.consume_approval = consume_approval
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.Lock()
        self._active = False
        self._consumed_approvals: set[str] = set()

    def execute(self, candidate: dict[str, Any], approval: dict[str, Any] | None, baseline: dict[str, Any]) -> dict[str, Any]:
        self.policy.validate_candidate(candidate)
        self._validate_approval(candidate, approval)
        self._validate_baseline(baseline)
        with self._lock:
            if self._active:
                raise RunnerError("Another experiment is already running.")
            approval_key = str((approval or {}).get("approval_id") or (approval or {}).get("candidate_hash"))
            if approval_key in self._consumed_approvals:
                raise SafetyViolation("Approval has already been consumed.")
            if self.consume_approval is not None:
                self.consume_approval(approval or {})
            self._consumed_approvals.add(approval_key)
            self._active = True
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        created_at = datetime.now(timezone.utc).isoformat()
        run: dict[str, Any] = {
            "schema_version": "1.0",
            "run_id": run_id,
            "experiment_id": candidate["id"],
            "status": "running",
            "created_at": created_at,
            "prediction": {"predicted_impacts": candidate["predicted_impacts"]},
            "observations": [],
            "cleanup_state": "not_started",
            "safety_violation": False,
        }
        target = candidate["target_component"]
        started = self.clock()
        stopped = False
        try:
            self.audit("safety_preflight_passed", {"run_id": run_id, "experiment_id": candidate["id"], "target": target})
            self.docker.preflight(self.policy.compose_project, target)
            self.docker.stop(self.policy.compose_project, target)
            stopped = True
            run["cleanup_state"] = "required"
            self.audit("fault_started", {"run_id": run_id, "action": candidate["action"], "target": target})
            self.sleep(candidate["duration_seconds"])
            observation = self.observations.observe()
            run["observations"].append(observation)
            decision = KillSwitch(baseline, self.policy).evaluate(observation, self.clock() - started)
            if decision.aborted:
                run["status"] = "aborted"
                run["abort_reason"] = decision.reason
                self.audit("experiment_aborted", {"run_id": run_id, "reason": decision.reason})
            else:
                run["status"] = "recovering"
        except Exception as error:
            run["status"] = "failed"
            run["error"] = type(error).__name__
            self.audit("runner_error", {"run_id": run_id, "error": type(error).__name__})
        finally:
            try:
                if stopped:
                    self.docker.start(self.policy.compose_project, target)
                run["cleanup_state"] = "complete"
                self.audit("cleanup_complete", {"run_id": run_id, "target": target})
            except Exception as error:
                run["cleanup_state"] = "failed"
                run["status"] = "failed"
                run["error"] = type(error).__name__
                self.audit("cleanup_failed", {"run_id": run_id, "error": type(error).__name__})
        if run["status"] == "recovering":
            try:
                recovery = self.recovery.verify(target)
                run["recovery"] = recovery
                run["recovery_verified"] = bool(recovery.get("healthy") and recovery.get("canary"))
                run["status"] = "completed" if run["recovery_verified"] else "failed"
                run["recovery_time_seconds"] = max(0.0, self.clock() - started)
                self.audit("recovery_checked", {"run_id": run_id, "verified": run["recovery_verified"]})
            except Exception as error:
                run["status"] = "failed"
                run["error"] = type(error).__name__
                run["recovery_verified"] = False
                self.audit("recovery_failed", {"run_id": run_id, "error": type(error).__name__})
        with self._lock:
            self._active = False
        run["resilience_score"] = calculate_resilience_score(run)
        return run

    def _validate_approval(self, candidate: dict[str, Any], approval: dict[str, Any] | None) -> None:
        if not self.policy.approval_required:
            return
        if not approval or approval.get("decision") != "approve":
            raise SafetyViolation("A human approval is required before execution.")
        if approval.get("candidate_hash") != self.candidate_hash(candidate):
            raise SafetyViolation("Approval is not bound to the immutable candidate.")
        expires_at = approval.get("expires_at")
        try:
            expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except (TypeError, ValueError) as error:
            raise SafetyViolation("Approval expiry is invalid.") from error
        if expires.tzinfo is None or expires.utcoffset() is None:
            raise SafetyViolation("Approval expiry must include a timezone.")
        if expires <= datetime.now(timezone.utc):
            raise SafetyViolation("Approval has expired.")

    @staticmethod
    def candidate_hash(candidate: dict[str, Any]) -> str:
        payload = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _validate_baseline(baseline: dict[str, Any]) -> None:
        if baseline.get("telemetry_status") != "available" or not baseline.get("baseline_id"):
            raise SafetyViolation("A healthy baseline with telemetry coverage is required.")