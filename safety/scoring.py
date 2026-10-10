from __future__ import annotations

from typing import Any
import math


WEIGHTS = {"availability": 0.30, "latency": 0.20, "recovery": 0.20, "containment": 0.15, "detection": 0.15}


def _finite(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def derive_score_components(run: dict[str, Any], recovery_budget_seconds: float = 180.0) -> dict[str, float]:
    """Derive score inputs from recorded fault observations without inventing data."""
    if run.get("dry_run") or run.get("forced_test"):
        return {}
    observations = [item for item in run.get("observations", []) if item.get("phase") == "fault"]
    if not observations:
        return {}
    error_rates = [value for item in observations for value in (_finite(item.get("write_error_rate")), _finite(item.get("search_error_rate"))) if value is not None]
    # Write p95 only: the baseline is a write p95, and search is normally slower than writes.
    p95_values = [value for item in observations if (value := _finite(item.get("write_p95_ms"))) is not None]
    baseline_p95 = _finite(run.get("baseline_write_p95_ms"))
    components: dict[str, float] = {}
    if error_rates:
        components["availability"] = max(0.0, min(1.0, 1.0 - max(error_rates)))
    if baseline_p95 and p95_values:
        components["latency"] = max(0.0, min(1.0, baseline_p95 / max(p95_values)))
    recovery_time = _finite(run.get("recovery_time_seconds"))
    if run.get("recovery_verified") is True and recovery_time is not None and recovery_budget_seconds > 0:
        components["recovery"] = max(0.0, min(1.0, 1.0 - recovery_time / recovery_budget_seconds))
    health_values = [item.get("unrelated_component_healthy") for item in observations if "unrelated_component_healthy" in item]
    if health_values:
        components["containment"] = 1.0 if all(value is True for value in health_values) else 0.0
    if run.get("status") == "completed":
        components["detection"] = 1.0
    elif run.get("abort_reason"):
        components["detection"] = 0.5
    return components


def calculate_resilience_score(run: dict[str, Any]) -> dict[str, Any]:
    if run.get("dry_run"):
        return {"status": "not_scored_dry_run", "score": None, "coverage": 0.0, "components": {}}
    if run.get("safety_violation"):
        return {"status": "unsafe", "score": None, "coverage": 0.0, "components": {}}
    if run.get("forced_test"):
        # A forced kill-switch test proves the abort path; it is not resilience evidence.
        return {"status": "not_scored_forced_test", "score": None, "coverage": 0.0, "components": {}}
    values = run.get("score_components", {})
    observed = {}
    for name, value in values.items():
        if value is None or name not in WEIGHTS:
            continue
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric_value):
            observed[name] = max(0.0, min(1.0, numeric_value))
    weight = sum(WEIGHTS[name] for name in observed)
    if weight < 0.80 or not run.get("baseline_valid", True) or run.get("recovery_verified") is False:
        return {"status": "incomplete", "score": None, "coverage": weight, "components": observed}
    score = 100 * sum(WEIGHTS[name] * value for name, value in observed.items()) / weight
    return {"status": "complete", "score": round(score, 2), "coverage": weight, "components": observed}