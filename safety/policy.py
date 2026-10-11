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
SUPPORTED_ACTIONS = frozenset({"restart_container", "stop_container", "pause_container", "cpu_throttle", "traffic_burst"})

# Hard bounds on action parameters, whatever the policy file says.
PARAM_BOUNDS = {
    "cpus": (0.05, 4.0),             # cpu_throttle: CPUs left to the target
    "rate_per_second": (1.0, 50.0),  # traffic_burst: requests/s per probe loop
    "vehicles": (1, 50),             # traffic_burst: vehicles exercised
}


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
class CompoundPolicy:
    enabled: bool = False
    max_faults: int = 2
    max_stagger_seconds: int = 20
    max_risk_score: float = 0.0
    allowed_pairs: frozenset[frozenset[str]] = frozenset()

    @classmethod
    def from_mapping(cls, raw: Any) -> "CompoundPolicy":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise SafetyViolation("compound must be a mapping.")
        pairs = set()
        for pair in raw.get("allowed_pairs", []) or []:
            if (not isinstance(pair, list) or len(pair) != 2
                    or not all(isinstance(item, str) and item.count(":") == 1 for item in pair) or pair[0] == pair[1]):
                raise SafetyViolation("compound.allowed_pairs entries must be two distinct 'action:target' strings.")
            pairs.add(frozenset(pair))
        try:
            policy = cls(
                enabled=bool(raw.get("enabled", False)),
                max_faults=int(raw.get("max_faults", 2)),
                max_stagger_seconds=int(raw.get("max_stagger_seconds", 20)),
                max_risk_score=float(raw.get("max_risk_score", 0.0)),
                allowed_pairs=frozenset(pairs),
            )
        except (TypeError, ValueError) as error:
            raise SafetyViolation("compound contains a non-numeric value.") from error
        # Hard ceiling: this control plane only ever combines two faults.
        if policy.enabled and (policy.max_faults != 2 or policy.max_stagger_seconds < 0 or not 0 < policy.max_risk_score <= 1):
            raise SafetyViolation("compound limits are outside the hard bounds (max_faults=2, risk in (0,1]).")
        return policy


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
    # Per-action target lists. An action with its own list may target only those services;
    # a denied target is allowed only where an action lists it explicitly (e.g. mongodb for
    # cpu_throttle). Actions without a list use allowed_targets.
    action_targets: tuple[tuple[str, frozenset[str]], ...] = ()
    action_params: tuple[tuple[str, tuple[tuple[str, float], ...]], ...] = ()
    compound: CompoundPolicy = CompoundPolicy()

    def targets_for(self, action: str) -> frozenset[str]:
        return dict(self.action_targets).get(action, self.allowed_targets)

    def params_for(self, action: str) -> dict[str, float]:
        return dict(dict(self.action_params).get(action, ()))

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
        action_targets = []
        action_params = []
        for name in sorted(enabled_actions):
            config = actions.get(name, {})
            if "targets" in config:
                targets = config.get("targets") or []
                if not isinstance(targets, list) or not all(isinstance(item, str) and item for item in targets):
                    raise SafetyViolation(f"actions.{name}.targets must be a list of service names.")
                action_targets.append((name, frozenset(targets)))
            bounded = []
            for key, (low, high) in PARAM_BOUNDS.items():
                if key in config:
                    try:
                        value = float(config[key])
                    except (TypeError, ValueError) as error:
                        raise SafetyViolation(f"actions.{name}.{key} must be a number.") from error
                    if not low <= value <= high:
                        raise SafetyViolation(f"actions.{name}.{key}={value} is outside the hard bounds {low}..{high}.")
                    bounded.append((key, value))
            action_params.append((name, tuple(bounded)))
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
            action_targets=tuple(action_targets),
            action_params=tuple(action_params),
            abort_conditions=tuple(str(item) for item in raw.get("abort_conditions", [])),
            thresholds=AbortThresholds.from_mapping(raw.get("abort_thresholds")),
            recovery_timeout_seconds=float(recovery.get("timeout_seconds", 180)),
            recovery_poll_interval_seconds=float(recovery.get("poll_interval_seconds", 3)),
            watchdog_grace_seconds=float(watchdog.get("grace_seconds", 15)),
            compound=CompoundPolicy.from_mapping(raw.get("compound")),
        )
        for pair in policy.compound.allowed_pairs:
            for item in pair:
                action, target = item.split(":")
                if action not in policy.enabled_actions:
                    raise SafetyViolation(f"compound pair uses an action that is not enabled: {action}.")
                if target not in policy.targets_for(action):
                    raise SafetyViolation(f"compound pair target {target} is not allowlisted for {action}.")
                if target == "mongodb":
                    raise SafetyViolation("MongoDB can never be part of a combined failure.")
        if not policy.compose_project:
            raise SafetyViolation("Policy must name the local Compose project.")
        if policy.recovery_timeout_seconds <= 0 or policy.recovery_poll_interval_seconds <= 0 or policy.watchdog_grace_seconds <= 0:
            raise SafetyViolation("Recovery and watchdog timings must be positive.")
        return policy

    def _validate_fault(self, action: Any, target: Any) -> None:
        if not isinstance(action, str) or action not in self.enabled_actions:
            raise SafetyViolation("Action is not enabled by the local experiment policy.")
        explicit = dict(self.action_targets).get(action)
        if not isinstance(target, str) or target not in self.targets_for(action):
            raise SafetyViolation("Target is not allowlisted for this action by the local experiment policy.")
        if target in self.denied_targets and (explicit is None or target not in explicit):
            raise SafetyViolation("Target is denied by the local experiment policy.")

    def validate_compound(self, candidate: dict[str, Any]) -> None:
        """A combined failure: staged faults, one approval, one kill switch, bounded as a whole."""
        rules = self.compound
        if not rules.enabled:
            raise SafetyViolation("Combined failures are not enabled by the local experiment policy.")
        faults = candidate.get("faults")
        if not isinstance(faults, list) or not 2 <= len(faults) <= rules.max_faults:
            raise SafetyViolation(f"A combined failure needs 2..{rules.max_faults} faults.")
        seen_targets: set[str] = set()
        keys: list[str] = []
        end = 0
        risk = 1.0
        for fault in faults:
            if not isinstance(fault, dict):
                raise SafetyViolation("Each fault must be a mapping.")
            action, target = fault.get("action"), fault.get("target_component")
            self._validate_fault(action, target)
            if target in seen_targets:
                raise SafetyViolation("Two faults on one target are not allowed in a combined failure.")
            seen_targets.add(target)
            keys.append(f"{action}:{target}")
            duration, offset = fault.get("duration_seconds"), fault.get("start_offset_seconds")
            for value, low in ((duration, 1), (offset, 0)):
                if not isinstance(value, int) or isinstance(value, bool) or value < low:
                    raise SafetyViolation("Fault duration and start offset must be whole seconds within policy bounds.")
            if offset > rules.max_stagger_seconds:
                raise SafetyViolation("Fault start offset exceeds the combined-failure stagger limit.")
            end = max(end, offset + duration)
            single = fault.get("risk_score")
            if not isinstance(single, (int, float)) or isinstance(single, bool) or not math.isfinite(single) or not 0 <= single <= 1:
                raise SafetyViolation("Each fault needs a finite risk score between 0 and 1.")
            risk *= 1.0 - float(single)
        if "mongodb" in seen_targets:
            raise SafetyViolation("MongoDB can never be part of a combined failure.")
        if frozenset(keys) not in rules.allowed_pairs:
            raise SafetyViolation("This combination of faults is not on the policy's allowed_pairs list.")
        if candidate.get("duration_seconds") != end or end > self.max_duration_seconds:
            raise SafetyViolation("Combined duration must equal the last fault's end and stay within the policy limit.")
        combined = 1.0 - risk
        stated = candidate.get("risk_score")
        if not isinstance(stated, (int, float)) or isinstance(stated, bool) or abs(stated - combined) > 0.01:
            raise SafetyViolation("Combined risk score does not match 1-prod(1-r) of its faults.")
        if combined > rules.max_risk_score:
            raise SafetyViolation("Combined risk exceeds the policy limit.")
        if candidate.get("approval_required") is not True:
            raise SafetyViolation("Human approval is required for every experiment.")
        if not candidate.get("hypothesis") or not candidate.get("predicted_impacts"):
            raise SafetyViolation("Experiment hypothesis and predicted impacts are required.")

    def validate_candidate(self, candidate: dict[str, Any]) -> None:
        if candidate.get("kind") == "compound":
            self.validate_compound(candidate)
            return
        target = candidate.get("target_component")
        action = candidate.get("action")
        duration = candidate.get("duration_seconds")
        risk = candidate.get("risk_score")
        self._validate_fault(action, target)
        if not isinstance(duration, int) or isinstance(duration, bool) or duration < 1 or duration > self.max_duration_seconds:
            raise SafetyViolation("Experiment duration exceeds the policy limit.")
        if not isinstance(risk, (int, float)) or isinstance(risk, bool) or not math.isfinite(risk) or risk < 0 or risk > self.max_risk_score:
            raise SafetyViolation("Experiment risk exceeds the policy limit.")
        if candidate.get("approval_required") is not True:
            raise SafetyViolation("Human approval is required for every experiment.")
        if not candidate.get("hypothesis") or not candidate.get("predicted_impacts"):
            raise SafetyViolation("Experiment hypothesis and predicted impacts are required.")
