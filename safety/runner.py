from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import hashlib
import json
import threading
import time
import uuid
from typing import Any, Callable, Protocol

from .kill_switch import KillSwitch, baseline_write_p95
from .policy import Policy, SafetyViolation
from .scoring import calculate_resilience_score, score_breakdown


class RunnerError(RuntimeError):
    """Raised when a local experiment cannot be executed safely."""


class DockerAdapter(Protocol):
    def preflight(self, target: str) -> None: ...
    def capture(self, action: str, target: str) -> dict[str, Any]: ...
    def apply(self, action: str, target: str, params: dict[str, Any] | None = None, state: dict[str, Any] | None = None) -> None: ...
    def rollback(self, action: str, target: str, state: dict[str, Any] | None = None) -> None: ...


class ObservationProvider(Protocol):
    def observe(self, target: "str | tuple[str, ...]", since: float | None = None) -> dict[str, Any]: ...


class RecoveryChecker(Protocol):
    def verify(self, target: str) -> dict[str, Any]: ...


class Watchdog(Protocol):
    def arm(self, target: str, deadline_epoch: float, action: str = "restart_container", state: dict[str, Any] | None = None) -> Any: ...
    def disarm(self, handle: Any) -> None: ...


# Forced kill-switch test (CLAUDE.md M4: "a forced abort proving the kill switch").
# During the fault only, the write-p95 limit drops to 1% of baseline, so any real write
# latency must trip the real kill switch through the real rollback path. Steady state
# and recovery keep the normal policy thresholds.
FORCED_TEST_P95_MULTIPLIER = 0.01


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class ExperimentRunner:
    """Deterministic fault runner. Runs only in the operator process (safety.cli), never in the API.

    Order: validate -> preflight -> steady-state check -> arm watchdog -> inject ->
    poll kill switch every poll_interval_seconds (abort = immediate rollback) ->
    rollback -> recovery check (also after aborts) -> disarm watchdog.
    """

    def __init__(
        self,
        policy: Policy,
        docker: DockerAdapter,
        observations: ObservationProvider,
        recovery: RecoveryChecker,
        watchdog: Watchdog,
        audit: Callable[[str, dict[str, Any]], None] | None = None,
        consume_approval: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.policy = policy
        self.docker = docker
        self.observations = observations
        self.recovery = recovery
        self.watchdog = watchdog
        self.audit = audit or (lambda _event, _payload: None)
        self.consume_approval = consume_approval
        self.clock = clock
        self.wall_clock = wall_clock
        self.sleep = sleep
        self._lock = threading.Lock()
        self._active = False
        self._consumed_approvals: set[str] = set()

    def execute(
        self,
        candidate: dict[str, Any],
        approval: dict[str, Any] | None,
        baseline: dict[str, Any],
        *,
        forced_test: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self.policy.validate_candidate(candidate)  # also validates a combined failure
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
        try:
            if candidate.get("kind") == "compound":
                run = self._run_compound(candidate, baseline, forced_test)
            else:
                run = self._run(candidate, baseline, forced_test)
        finally:
            with self._lock:
                self._active = False
        if dry_run:
            run["dry_run"] = True
        run["score_components"], run["score_inputs"] = score_breakdown(run, self.policy.recovery_timeout_seconds)
        run["resilience_score"] = calculate_resilience_score(run)
        return run

    def _run(self, candidate: dict[str, Any], baseline: dict[str, Any], forced_test: bool = False) -> dict[str, Any]:
        thresholds = self.policy.thresholds
        target = candidate["target_component"]
        action = candidate["action"]
        duration = candidate["duration_seconds"]
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        timeline: dict[str, str] = {"created_at": _iso(self.wall_clock())}
        run: dict[str, Any] = {
            "schema_version": "1.0",
            "run_id": run_id,
            "experiment_id": candidate["id"],
            "status": "running",
            "created_at": timeline["created_at"],
            "prediction": {
                "predicted_impacts": candidate["predicted_impacts"],
                **({"predicted_slis": candidate["predicted_slis"]} if candidate.get("predicted_slis") else {}),
            },
            "observations": [],
            "cleanup_state": "not_started",
            "safety_violation": False,
            "timeline": timeline,
        }
        if forced_test:
            run["forced_test"] = True
            run["forced_test_rule"] = (
                f"write p95 limit lowered to {FORCED_TEST_P95_MULTIPLIER}x baseline during the fault only"
            )
        injected = False
        watchdog_handle: Any = None
        effective_baseline = baseline
        try:
            self.audit("safety_preflight_started", {"run_id": run_id, "experiment_id": candidate["id"], "target": target})
            self.docker.preflight(target)

            # Steady state: the system must be healthy *now*, measured with real SLI samples.
            pre = {**self.observations.observe(target), "phase": "pre_fault"}
            run["observations"].append(pre)
            if baseline_write_p95(baseline) is None:
                effective_baseline = {**baseline, "sli": {"write_p95_ms": pre.get("write_p95_ms")}, "sli_source": "pre_fault_window"}
            run["baseline_write_p95_ms"] = baseline_write_p95(effective_baseline)
            kill_switch = KillSwitch(effective_baseline, self.policy)
            steady = kill_switch.evaluate(pre, 0.0, enforce_deadline=False)
            if steady.aborted:
                run["status"] = "aborted"
                run["abort_reason"] = f"steady_state_not_met:{steady.reason}"
                run["baseline_valid"] = False
                self.audit("experiment_refused", {"run_id": run_id, "reason": run["abort_reason"]})
                return run

            fault_switch = kill_switch
            if forced_test:
                forced_policy = dataclasses.replace(
                    self.policy,
                    thresholds=dataclasses.replace(self.policy.thresholds, write_p95_multiplier=FORCED_TEST_P95_MULTIPLIER),
                )
                fault_switch = KillSwitch(effective_baseline, forced_policy)

            # Capture what the rollback must restore (e.g. the original CPU quota).
            action_state = self.docker.capture(action, target)
            run["action_state"] = action_state
            params = {**self.policy.params_for(action), "duration_seconds": duration}
            run["action_params"] = params

            # Arm the independent watchdog before touching the target.
            deadline_epoch = self.wall_clock() + duration + self.policy.watchdog_grace_seconds
            watchdog_handle = self.watchdog.arm(target, deadline_epoch, action, action_state)
            run["watchdog"] = {"armed": True, "deadline": _iso(deadline_epoch)}

            # Mark the fault as injected *before* the command runs: `docker compose restart`
            # stops the target immediately and only returns after the start (~8 s), and if the
            # command fails half-way the target may already be stopped. Cleanup must then run.
            injected = True
            run["cleanup_state"] = "required"
            started = self.clock()
            timeline["injected_at"] = _iso(self.wall_clock())
            self.audit("fault_started", {"run_id": run_id, "action": action, "target": target})
            self.docker.apply(action, target, params, action_state)
            timeline["action_completed_at"] = _iso(self.wall_clock())

            # Live kill switch: evaluate every poll interval, roll back on the first abort.
            while (elapsed := self.clock() - started) < duration:
                self.sleep(min(thresholds.poll_interval_seconds, duration - elapsed))
                observation = {**self.observations.observe(target), "phase": "fault"}
                run["observations"].append(observation)
                decision = fault_switch.evaluate(observation, self.clock() - started)
                if decision.aborted:
                    run["status"] = "aborted"
                    run["abort_reason"] = decision.reason
                    timeline["abort_at"] = _iso(self.wall_clock())
                    self.audit("experiment_aborted", {"run_id": run_id, "reason": decision.reason})
                    break
            if forced_test:
                # The test passes only if the kill switch fired during the fault *because of the
                # lowered limit*. An abort for any other reason (e.g. missing samples) does not
                # prove the threshold path.
                run["forced_test_passed"] = (
                    run["status"] == "aborted" and run.get("abort_reason") == "p95_latency_above_baseline_multiplier"
                )
            if run["status"] == "running":
                run["status"] = "recovering"
        except Exception as error:  # noqa: BLE001 - any failure must still reach cleanup
            run["status"] = "failed"
            run["error"] = type(error).__name__
            run["error_detail"] = str(error)[:300]
            self.audit("runner_error", {"run_id": run_id, "error": type(error).__name__})
        finally:
            rollback_ok = self._rollback(run, target, injected, watchdog_handle, action, run.get("action_state"))

        if injected and rollback_ok:
            self._verify_recovery(run, target, kill_switch)
        run["baseline_valid"] = run.get("baseline_valid", True)
        return run

    def _run_compound(self, candidate: dict[str, Any], baseline: dict[str, Any], forced_test: bool = False) -> dict[str, Any]:
        """Staged combined failure: each fault starts at its own offset and ends at its own duration.

        One kill switch watches the whole window. Any abort rolls every active fault back in
        reverse injection order; each rollback is attempted even if another one fails, and the
        watchdog of a fault whose rollback failed stays armed.
        """
        thresholds = self.policy.thresholds
        faults = sorted((dict(item) for item in candidate["faults"]), key=lambda item: item["start_offset_seconds"])
        targets = tuple(item["target_component"] for item in faults)
        total = candidate["duration_seconds"]
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        timeline: dict[str, str] = {"created_at": _iso(self.wall_clock())}
        run: dict[str, Any] = {
            "schema_version": "1.0",
            "run_id": run_id,
            "experiment_id": candidate["id"],
            "kind": "compound",
            "status": "running",
            "created_at": timeline["created_at"],
            "prediction": {
                "predicted_impacts": candidate["predicted_impacts"],
                **({"predicted_slis": candidate["predicted_slis"]} if candidate.get("predicted_slis") else {}),
            },
            "observations": [],
            "faults": [],
            "cleanup_state": "not_started",
            "safety_violation": False,
            "timeline": timeline,
        }
        if forced_test:
            run["forced_test"] = True
            run["forced_test_rule"] = (
                f"write p95 limit lowered to {FORCED_TEST_P95_MULTIPLIER}x baseline during the fault only"
            )
        records: list[dict[str, Any]] = []
        for fault in faults:
            records.append({
                "action": fault["action"],
                "target": fault["target_component"],
                "start_offset_seconds": fault["start_offset_seconds"],
                "duration_seconds": fault["duration_seconds"],
                "state": "planned",
                "handle": None,
                "action_state": {},
            })
        injected_order: list[dict[str, Any]] = []
        kill_switch: KillSwitch | None = None
        try:
            self.audit("safety_preflight_started", {"run_id": run_id, "experiment_id": candidate["id"], "targets": list(targets)})
            for target in targets:
                self.docker.preflight(target)
            pre = {**self.observations.observe(targets), "phase": "pre_fault", "active_faults": []}
            run["observations"].append(pre)
            effective_baseline = baseline
            if baseline_write_p95(baseline) is None:
                effective_baseline = {**baseline, "sli": {"write_p95_ms": pre.get("write_p95_ms")}, "sli_source": "pre_fault_window"}
            run["baseline_write_p95_ms"] = baseline_write_p95(effective_baseline)
            kill_switch = KillSwitch(effective_baseline, self.policy)
            steady = kill_switch.evaluate(pre, 0.0, enforce_deadline=False)
            if steady.aborted:
                run["status"] = "aborted"
                run["abort_reason"] = f"steady_state_not_met:{steady.reason}"
                run["baseline_valid"] = False
                self.audit("experiment_refused", {"run_id": run_id, "reason": run["abort_reason"]})
                return run
            fault_switch = kill_switch
            if forced_test:
                forced_policy = dataclasses.replace(
                    self.policy,
                    thresholds=dataclasses.replace(self.policy.thresholds, write_p95_multiplier=FORCED_TEST_P95_MULTIPLIER),
                )
                fault_switch = KillSwitch(effective_baseline, forced_policy)

            # Capture and arm everything BEFORE touching any target.
            for record in records:
                record["action_state"] = self.docker.capture(record["action"], record["target"])
                record["params"] = {**self.policy.params_for(record["action"]), "duration_seconds": record["duration_seconds"]}
                deadline_epoch = self.wall_clock() + record["start_offset_seconds"] + record["duration_seconds"] + self.policy.watchdog_grace_seconds
                record["handle"] = self.watchdog.arm(record["target"], deadline_epoch, record["action"], record["action_state"])
                record["watchdog_deadline"] = _iso(deadline_epoch)
            run["watchdog"] = {"armed": True, "per_fault": [item["watchdog_deadline"] for item in records]}

            started = self.clock()
            pending = list(records)
            while True:
                elapsed = self.clock() - started
                # Inject every fault whose start offset has arrived (marked before the command runs).
                for record in [item for item in pending if item["start_offset_seconds"] <= elapsed]:
                    pending.remove(record)
                    record["state"] = "injected"
                    record["injected_at"] = _iso(self.wall_clock())
                    record["_started_mono"] = self.clock()
                    injected_order.append(record)
                    run["cleanup_state"] = "required"
                    timeline.setdefault("injected_at", record["injected_at"])
                    self.audit("fault_started", {"run_id": run_id, "action": record["action"], "target": record["target"]})
                    self.docker.apply(record["action"], record["target"], record["params"], record["action_state"])
                # A fault that reached its own end rolls back now; the others keep running.
                elapsed = self.clock() - started
                for record in [item for item in injected_order if item["state"] == "injected"
                               and elapsed >= item["start_offset_seconds"] + item["duration_seconds"]]:
                    self._rollback_fault(run, record)
                if elapsed >= total:
                    break
                next_events = [item["start_offset_seconds"] - elapsed for item in pending]
                next_events += [item["start_offset_seconds"] + item["duration_seconds"] - elapsed
                                for item in injected_order if item["state"] == "injected"]
                wait = min([thresholds.poll_interval_seconds, total - elapsed, *[max(0.0, event) for event in next_events]])
                self.sleep(max(0.0, wait))
                observation = {
                    **self.observations.observe(targets),
                    "phase": "fault",
                    "active_faults": [f"{item['action']}:{item['target']}" for item in injected_order if item["state"] == "injected"],
                }
                run["observations"].append(observation)
                decision = fault_switch.evaluate(observation, self.clock() - started)
                if decision.aborted:
                    run["status"] = "aborted"
                    run["abort_reason"] = decision.reason
                    timeline["abort_at"] = _iso(self.wall_clock())
                    self.audit("experiment_aborted", {"run_id": run_id, "reason": decision.reason})
                    break
            if forced_test:
                run["forced_test_passed"] = (
                    run["status"] == "aborted" and run.get("abort_reason") == "p95_latency_above_baseline_multiplier"
                )
            if run["status"] == "running":
                run["status"] = "recovering"
        except Exception as error:  # noqa: BLE001 - any failure must still reach cleanup
            run["status"] = "failed"
            run["error"] = type(error).__name__
            run["error_detail"] = str(error)[:300]
            self.audit("runner_error", {"run_id": run_id, "error": type(error).__name__})
        finally:
            rollback_ok = self._rollback_all(run, records, injected_order)

        if injected_order and rollback_ok and kill_switch is not None:
            self._verify_recovery(run, targets, kill_switch)
            # recovery_time_seconds counts from the LAST rollback; per fault it counts from that
            # fault's own rollback, which is what compares like-for-like with a solo run.
            recovered = timeline.get("recovered_at")
            if recovered:
                end = datetime.fromisoformat(recovered)
                for record in records:
                    if record.get("rolled_back_at"):
                        record["recovery_after_own_rollback_seconds"] = round(
                            (end - datetime.fromisoformat(record["rolled_back_at"])).total_seconds(), 3)
        run["target_component"] = "+".join(targets)
        run["baseline_valid"] = run.get("baseline_valid", True)
        run["faults"] = [
            {key: value for key, value in record.items() if key not in ("handle", "_started_mono")}
            for record in records
        ]
        return run

    def _rollback_fault(self, run: dict[str, Any], record: dict[str, Any]) -> bool:
        """Undo one fault. Idempotent per record; leaves its watchdog armed if the rollback fails."""
        if record["state"] != "injected":
            return record["state"] == "rolled_back"
        try:
            self.docker.rollback(record["action"], record["target"], record["action_state"])
        except Exception as error:  # noqa: BLE001
            record["state"] = "rollback_failed"
            record["error"] = type(error).__name__
            self.audit("cleanup_failed", {"run_id": run["run_id"], "target": record["target"], "error": type(error).__name__, "watchdog": "left_armed"})
            return False
        record["state"] = "rolled_back"
        record["rolled_back_at"] = _iso(self.wall_clock())
        if record["handle"] is not None:
            self.watchdog.disarm(record["handle"])
        self.audit("cleanup_complete", {"run_id": run["run_id"], "target": record["target"]})
        return True

    def _rollback_all(self, run: dict[str, Any], records: list[dict[str, Any]], injected_order: list[dict[str, Any]]) -> bool:
        """Reverse injection order; every rollback is attempted even if an earlier one failed."""
        timeline = run["timeline"]
        for record in records:
            if record["state"] == "planned" and record["handle"] is not None:
                self.watchdog.disarm(record["handle"])  # never injected: nothing to undo
        if not injected_order:
            if "watchdog" in run:
                run["watchdog"]["armed"] = False
            return True
        results = [self._rollback_fault(run, record) for record in reversed(injected_order)]
        ok = all(results)
        if ok:
            run["cleanup_state"] = "complete"
            timeline["rollback_at"] = _iso(self.wall_clock())
            run["_rollback_wall"] = self.wall_clock()
            run["_rollback_mono"] = self.clock()
            run["watchdog"]["armed"] = False
        else:
            run["cleanup_state"] = "failed"
            run["status"] = "failed"
        return ok

    def _rollback(
        self,
        run: dict[str, Any],
        target: str,
        injected: bool,
        watchdog_handle: Any,
        action: str = "restart_container",
        action_state: dict[str, Any] | None = None,
    ) -> bool:
        timeline = run["timeline"]
        if not injected:
            if watchdog_handle is not None:
                self.watchdog.disarm(watchdog_handle)
                run["watchdog"]["armed"] = False
            return True
        try:
            self.docker.rollback(action, target, action_state)
        except Exception as error:  # noqa: BLE001
            run["cleanup_state"] = "failed"
            run["status"] = "failed"
            run["error"] = type(error).__name__
            # Leave the watchdog armed: it retries the cleanup independently.
            self.audit("cleanup_failed", {"run_id": run["run_id"], "error": type(error).__name__, "watchdog": "left_armed"})
            return False
        run["cleanup_state"] = "complete"
        timeline["rollback_at"] = _iso(self.wall_clock())
        run["_rollback_wall"] = self.wall_clock()
        run["_rollback_mono"] = self.clock()
        if watchdog_handle is not None:
            self.watchdog.disarm(watchdog_handle)
            run["watchdog"]["armed"] = False
        self.audit("cleanup_complete", {"run_id": run["run_id"], "target": target})
        return True

    def _verify_recovery(self, run: dict[str, Any], target: "str | tuple[str, ...]", kill_switch: KillSwitch) -> None:
        """Recovered = API health + canary pass AND the SLIs since rollback are back within thresholds.

        A combined failure passes every target: each one's check must pass.
        """
        targets = (target,) if isinstance(target, str) else tuple(target)
        rollback_wall = run.pop("_rollback_wall")
        rollback_mono = run.pop("_rollback_mono")
        deadline = rollback_mono + self.policy.recovery_timeout_seconds
        last: dict[str, Any] = {}
        while True:
            checks = []
            for item in targets:
                try:
                    checks.append(self.recovery.verify(item))
                except Exception as error:  # noqa: BLE001
                    checks.append({"healthy": False, "canary": False, "error": type(error).__name__})
            check = checks[0] if len(checks) == 1 else {
                **checks[0],
                "target": "+".join(targets),
                "healthy": all(item.get("healthy") for item in checks),
                "canary": all(item.get("canary") for item in checks),
                "checks": checks,
            }
            observation = self.observations.observe(target, since=rollback_wall)
            slis = kill_switch.evaluate(observation, 0.0, enforce_deadline=False)
            last = {**check, "slis_within_baseline": not slis.aborted, "sli_reason": slis.reason}
            if check.get("healthy") and check.get("canary") and not slis.aborted:
                run["recovery_verified"] = True
                run["recovery_time_seconds"] = round(max(0.0, self.clock() - rollback_mono), 3)
                run["timeline"]["recovered_at"] = _iso(self.wall_clock())
                run["observations"].append({**observation, "phase": "recovered"})
                break
            if self.clock() >= deadline:
                run["recovery_verified"] = False
                run["observations"].append({**observation, "phase": "recovery_timeout"})
                break
            self.sleep(self.policy.recovery_poll_interval_seconds)
        run["recovery"] = last
        self.audit("recovery_checked", {"run_id": run["run_id"], "verified": run["recovery_verified"]})
        if run["status"] == "recovering":
            run["status"] = "completed" if run["recovery_verified"] else "failed"
        elif run["status"] == "aborted" and not run["recovery_verified"]:
            # Aborted AND not recovered: operator recovery is required.
            run["status"] = "failed"

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

    def _validate_baseline(self, baseline: dict[str, Any]) -> None:
        if baseline.get("telemetry_status") != "available" or not baseline.get("baseline_id"):
            raise SafetyViolation("A healthy baseline with telemetry coverage is required.")
        try:
            captured = datetime.fromisoformat(str(baseline.get("captured_at")).replace("Z", "+00:00"))
        except (TypeError, ValueError) as error:
            raise SafetyViolation("The baseline has no valid capture time.") from error
        if captured.tzinfo is None:
            raise SafetyViolation("The baseline capture time must include a timezone.")
        age = (datetime.now(timezone.utc) - captured).total_seconds()
        if age < 0 or age > self.policy.baseline_max_age_seconds:
            raise SafetyViolation("The baseline is stale; capture a new one before executing.")
