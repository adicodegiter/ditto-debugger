from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class AbortDecision:
    aborted: bool
    reason: str | None = None


# Every field the kill switch needs. A missing or non-numeric value is never read as
# healthy: it aborts with "missing_samples" (CLAUDE.md: missing samples = unhealthy).
REQUIRED_FIELDS = (
    "telemetry_available",
    "unrelated_component_healthy",
    "write_samples",
    "write_error_rate",
    "write_p95_ms",
    "freshness_samples",
    "freshness_p95_seconds",
)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _dig(record: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(record, dict):
            return None
        record = record.get(key)
    return record


def baseline_write_p95(baseline: dict[str, Any]) -> float | None:
    """Write p95 (ms) of a stored baseline.

    Accepts the runner's own shape (``sli.write_p95_ms``) and the shape stored by
    ``capture_baseline`` from ``monitor.window.baseline_from_metrics``
    (``telemetry.telemetry.write.latency_ms.p95``).
    """
    for path in (("sli", "write_p95_ms"), ("telemetry", "telemetry", "write", "latency_ms", "p95"), ("telemetry", "write", "latency_ms", "p95")):
        value = _finite(_dig(baseline, *path))
        if value is not None and value > 0:
            return value
    return None


class KillSwitch:
    """Pure abort logic. Thresholds come only from the policy, never from the observation."""

    def __init__(self, baseline: dict[str, Any], policy: Any) -> None:
        self.baseline = baseline
        self.policy = policy
        self.thresholds = policy.thresholds

    def evaluate(self, observation: dict[str, Any], elapsed_seconds: float, *, enforce_deadline: bool = True) -> AbortDecision:
        if enforce_deadline and elapsed_seconds > self.policy.max_duration_seconds:
            return AbortDecision(True, "experiment_deadline_exceeded")
        if not isinstance(observation, dict) or any(field not in observation for field in REQUIRED_FIELDS):
            return AbortDecision(True, "missing_samples")
        if observation.get("telemetry_available") is not True:
            return AbortDecision(True, "missing_samples")
        if observation.get("unrelated_component_healthy") is not True:
            return AbortDecision(True, "unrelated_component_unhealthy")

        write_samples = _finite(observation["write_samples"])
        error_rate = _finite(observation["write_error_rate"])
        if write_samples is None or error_rate is None or write_samples < self.thresholds.min_write_samples:
            return AbortDecision(True, "missing_samples")
        # Error rate first: if every write failed there is no latency to report, and the
        # right reason is the error rate, not missing samples.
        if error_rate > self.thresholds.write_error_rate:
            return AbortDecision(True, "error_rate_above_configured_limit")

        write_p95 = _finite(observation["write_p95_ms"])
        freshness_samples = _finite(observation["freshness_samples"])
        freshness_p95 = _finite(observation["freshness_p95_seconds"])
        if None in (write_p95, freshness_samples, freshness_p95):
            return AbortDecision(True, "missing_samples")
        if freshness_samples < self.thresholds.min_freshness_samples:
            return AbortDecision(True, "missing_samples")

        baseline_p95 = baseline_write_p95(self.baseline)
        if baseline_p95 is None:
            return AbortDecision(True, "baseline_unhealthy")

        if write_p95 > self.thresholds.write_p95_multiplier * baseline_p95:
            return AbortDecision(True, "p95_latency_above_baseline_multiplier")
        if freshness_p95 > self.thresholds.freshness_p95_seconds:
            return AbortDecision(True, "freshness_p95_above_limit")
        return AbortDecision(False)
