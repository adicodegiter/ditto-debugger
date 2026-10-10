from __future__ import annotations

from typing import Any
import math


WEIGHTS = {"availability": 0.30, "latency": 0.20, "recovery": 0.20, "containment": 0.15, "detection": 0.15}


def derive_score_components(run: dict[str, Any], recovery_budget_seconds: float = 180.0) -> dict[str, float]:
    """Derive score inputs from the runner's recorded observations only."""
    observations = [item for item in run.get("observations", []) if item.get("phase") == "fault"]
    if not observations:
        return {}
    error_rates = [float(item["write_error_rate"]) for item in observations if item.get("write_error_rate") is not None]
    p95_values = [float(item["write_p95_ms"]) for item in observations if item.get("write_p95_ms") is not None]
    baseline_p95 = run.get("baseline_write_p95_ms")
    components: dict[str, float] = {}
    if error_rates:
        components["availability"] = max(0.0, min(1.0, 1.0 - max(error_rates)))
    if baseline_p95 and p95_values and max(p95_values) >= 0:
        components["latency"] = max(0.0, min(1.0, float(baseline_p95) / max(p95_values)))
    recovery_time = run.get("recovery_time_seconds")
    if run.get("recovery_verified") is True and recovery_time is not None:
        components["recovery"] = max(0.0, min(1.0, 1.0 - float(recovery_time) / recovery_budget_seconds))
    health_values = [item.get("unrelated_component_healthy") for item in observations]
    if health_values:
        components["containment"] = 1.0 if all(value is True for value in health_values) else 0.0
    if run.get("status") == "completed":
        components["detection"] = 1.0
    elif run.get("abort_reason"):
        components["detection"] = 0.5
    return components


def calculate_resilience_score(run: dict[str, Any]) -> dict[str, Any]:
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