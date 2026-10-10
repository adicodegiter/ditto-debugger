from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

import yaml


class SafetyViolation(ValueError):
    """Raised when a plan or run violates a deterministic safety rule."""


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

    @classmethod
    def from_file(cls, path: Path) -> "Policy":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SafetyViolation("Experiment policy must be a mapping.")
        scope = raw.get("scope", {})
        approval = raw.get("approval", {})
        actions = raw.get("actions", {})
        if scope.get("environment") != "local-only":
            raise SafetyViolation("Only local-only experiment policies are executable.")
        return cls(
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
            enabled_actions=frozenset(
                str(name) for name, config in actions.items() if isinstance(config, dict) and config.get("enabled")
            ),
            abort_conditions=tuple(str(item) for item in raw.get("abort_conditions", [])),
        )

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
