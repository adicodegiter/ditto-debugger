from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class AbortDecision:
    aborted: bool
    reason: str | None = None


class KillSwitch:
    def __init__(self, baseline: dict[str, Any], policy: Any) -> None:
        self.baseline = baseline
        self.policy = policy

    def evaluate(self, observation: dict[str, Any], elapsed_seconds: float) -> AbortDecision:
        required = {"telemetry_available", "baseline_healthy", "unrelated_component_healthy", "error_rate"}
        if not required.issubset(observation):
            return AbortDecision(True, "missing_samples")
        if elapsed_seconds > self.policy.max_duration_seconds:
            return AbortDecision(True, "experiment_deadline_exceeded")
        if observation.get("baseline_healthy") is False:
            return AbortDecision(True, "baseline_unhealthy")
        if observation.get("unrelated_component_healthy") is False:
            return AbortDecision(True, "unrelated_component_unhealthy")
        if observation.get("telemetry_available") is False:
            return AbortDecision(True, "missing_samples")
        try:
            error_rate = float(observation["error_rate"])
        except (TypeError, ValueError):
            return AbortDecision(True, "missing_samples")
        if not math.isfinite(error_rate):
            return AbortDecision(True, "missing_samples")
        if error_rate > float(observation.get("error_rate_limit", 0.20)):
            return AbortDecision(True, "error_rate_above_configured_limit")
        baseline_p95 = observation.get("baseline_p95_latency")
        current_p95 = observation.get("p95_latency")
        if baseline_p95 is not None and current_p95 is not None and float(current_p95) > 3 * float(baseline_p95):
            return AbortDecision(True, "p95_latency_above_three_times_baseline")
        if observation.get("freshness_p95_seconds") is not None and float(observation["freshness_p95_seconds"]) > 30:
            return AbortDecision(True, "freshness_p95_above_30_seconds")
        return AbortDecision(False)