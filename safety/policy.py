from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

import yaml


class SafetyViolation(ValueError):
    """Raised when a plan or run violates a deterministic safety rule."""


# The only actions the runner can perform. Each maps to one fixed Docker Compose
# subcommand in safety/runner.py; a policy enabling anything else is rejected.
SUPPORTED_ACTIONS = frozenset({"restart_container", "stop_container", "pause_container"})


@dataclass(frozen=True)
class AbortThresholds:
    window_seconds: float = 30.0
    poll_interval_seconds: float = 2.0
    write_error_rate: float = 0.20
    write_p95_multiplier: float = 3.0
    freshness_p95_seconds: float = 30.0
    min_write_samples: int = 3
    min_freshness_samples: int = 1

    @classmethod
    def from_mapping(cls, raw: Any) -> "AbortThresholds":
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise SafetyViolation("abort_thresholds must be a mapping.")
        defaults = cls()
        try:
            thresholds = cls(
                window_seconds=float(raw.get("window_seconds", defaults.window_seconds)),
                poll_interval_seconds=float(raw.get("poll_interval_seconds", defaults.poll_interval_seconds)),
                write_error_rate=float(raw.get("write_error_rate", defaults.write_error_rate)),
                write_p95_multiplier=float(raw.get("write_p95_multiplier", defaults.write_p95_multiplier)),
                freshness_p95_seconds=float(raw.get("freshness_p95_seconds", defaults.freshness_p95_seconds)),
                min_write_samples=int(raw.get("min_write_samples", defaults.min_write_samples)),
                min_freshness_samples=int(raw.get("min_freshness_samples", defaults.min_freshness_samples)),
            )
        except (TypeError, ValueError) as error:
            raise SafetyViolation("abort_thresholds contains a non-numeric value.") from error
        numbers = (
            thresholds.window_seconds,
            thresholds.poll_interval_seconds,
            thresholds.write_error_rate,
            thresholds.write_p95_multiplier,
            thresholds.freshness_p95_seconds,
        )
        if not all(math.isfinite(value) and value > 0 for value in numbers):
            raise SafetyViolation("abort_thresholds values must be finite and positive.")
        if thresholds.write_error_rate >= 1:
            raise SafetyViolation("write_error_rate must be below 1.0 or the kill switch can never fire.")
        if thresholds.min_write_samples < 1 or thresholds.min_freshness_samples < 1:
            raise SafetyViolation("Minimum sample counts must be at least 1.")
        return thresholds


@dataclass(frozen=True)
class Policy:
    version: int
    compose_project: str
    allowed_targets: frozenset[str]
    denied_targets: frozenset[str]
    max_concurrent_experiments: int
    max_duration_seconds: int
    max_risk_score: float
    baseline_max_age_seconds: int
    approval_required: bool
    approval_expires_after_minutes: int
    enabled_actions: frozenset[str]
    abort_conditions: tuple[str, ...]
    thresholds: AbortThresholds = AbortThresholds()
    recovery_timeout_seconds: float = 180.0
    recovery_poll_interval_seconds: float = 3.0
    watchdog_grace_seconds: float = 15.0

    @classmethod
    def from_file(cls, path: Path) -> "Policy":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SafetyViolation("Experiment policy must be a mapping.")
        scope = raw.get("scope", {})
        approval = raw.get("approval", {})
        actions = raw.get("actions", {})
        recovery = raw.get("recovery", {}) or {}
        watchdog = raw.get("watchdog", {}) or {}
        if scope.get("environment") != "local-only":
            raise SafetyViolation("Only local-only experiment policies are executable.")
        enabled_actions = frozenset(
            str(name) for name, config in actions.items() if isinstance(config, dict) and config.get("enabled")
        )
        unsupported = enabled_actions - SUPPORTED_ACTIONS
        if unsupported:
            raise SafetyViolation(f"Policy enables unsupported actions: {sorted(unsupported)}.")
        policy = cls(
            version=int(raw.get("version", 0)),
            compose_project=str(scope.get("compose_project", "")),
            allowed_targets=frozenset(str(item) for item in scope.get("allowed_targets", [])),
            denied_targets=frozenset(str(item) for item in scope.get("denied_targets", [])),
            max_concurrent_experiments=int(raw.get("max_concurrent_experiments", 0)),
            max_duration_seconds=int(raw.get("max_duration_seconds", 0)),
            max_risk_score=float(raw.get("max_risk_score", 0)),
            baseline_max_age_seconds=int(raw.get("baseline_max_age_seconds", 0)),
            approval_required=bool(approval.get("required", True)),
            approval_expires_after_minutes=int(approval.get("expires_after_minutes", 0)),
            enabled_actions=enabled_actions,
            abort_conditions=tuple(str(item) for item in raw.get("abort_conditions", [])),
            thresholds=AbortThresholds.from_mapping(raw.get("abort_thresholds")),
            recovery_timeout_seconds=float(recovery.get("timeout_seconds", 180)),
            recovery_poll_interval_seconds=float(recovery.get("poll_interval_seconds", 3)),
            watchdog_grace_seconds=float(watchdog.get("grace_seconds", 15)),
        )
        if not policy.compose_project:
            raise SafetyViolation("Policy must name the local Compose project.")
        if policy.recovery_timeout_seconds <= 0 or policy.recovery_poll_interval_seconds <= 0 or policy.watchdog_grace_seconds <= 0:
            raise SafetyViolation("Recovery and watchdog timings must be positive.")
        return policy

    def validate_candidate(self, candidate: dict[str, Any]) -> None:
        target = candidate.get("target_component")
        action = candidate.get("action")
        duration = candidate.get("duration_seconds")
        risk = candidate.get("risk_score")
        if not isinstance(target, str) or target not in self.allowed_targets or target in self.denied_targets:
            raise SafetyViolation("Target is not allowlisted by the local experiment policy.")
        if not isinstance(action, str) or action not in self.enabled_actions:
            raise SafetyViolation("Action is not enabled by the local experiment policy.")
        if not isinstance(duration, int) or isinstance(duration, bool) or duration < 1 or duration > self.max_duration_seconds:
            raise SafetyViolation("Experiment duration exceeds the policy limit.")
        if not isinstance(risk, (int, float)) or isinstance(risk, bool) or not math.isfinite(risk) or risk < 0 or risk > self.max_risk_score:
            raise SafetyViolation("Experiment risk exceeds the policy limit.")
        if candidate.get("approval_required") is not True:
            raise SafetyViolation("Human approval is required for every experiment.")
        if not candidate.get("hypothesis") or not candidate.get("predicted_impacts"):
            raise SafetyViolation("Experiment hypothesis and predicted impacts are required.")
