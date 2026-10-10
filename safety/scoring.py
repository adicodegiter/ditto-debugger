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


SCORE_VERSION = "1.0"


def score_breakdown(run: dict[str, Any], recovery_budget_seconds: float = 180.0) -> tuple[dict[str, float], dict[str, Any]]:
    """Return (components, inputs). Both come only from recorded observations; nothing is invented.

    `inputs` holds the raw values behind each component so another person can recompute the score.
    """
    if run.get("dry_run") or run.get("forced_test"):
        return {}, {}
    observations = [item for item in run.get("observations", []) if item.get("phase") == "fault"]
    if not observations:
        return {}, {}
    write_errors = [v for item in observations if (v := _finite(item.get("write_error_rate"))) is not None]
    search_errors = [v for item in observations if (v := _finite(item.get("search_error_rate"))) is not None]
    # Write p95 only: the baseline is a write p95, and search is normally slower than writes.
    p95_values = [v for item in observations if (v := _finite(item.get("write_p95_ms"))) is not None]
    baseline_p95 = _finite(run.get("baseline_write_p95_ms"))
    components: dict[str, float] = {}
    inputs: dict[str, Any] = {"score_version": SCORE_VERSION, "weights": dict(WEIGHTS), "fault_windows": len(observations)}
    error_rates = write_errors + search_errors
    if error_rates:
        components["availability"] = max(0.0, min(1.0, 1.0 - max(error_rates)))
        inputs["availability"] = {
            "rule": "1 - max(worst write error rate, worst search error rate) over fault windows",
            "worst_write_error_rate": max(write_errors) if write_errors else None,
            "worst_search_error_rate": max(search_errors) if search_errors else None,
        }
    if baseline_p95 and p95_values:
        components["latency"] = max(0.0, min(1.0, baseline_p95 / max(p95_values)))
        inputs["latency"] = {"rule": "baseline write p95 / worst write p95 during fault, capped at 1",
                             "baseline_write_p95_ms": baseline_p95, "worst_write_p95_ms": max(p95_values)}
    recovery_time = _finite(run.get("recovery_time_seconds"))
    if run.get("recovery_verified") is True and recovery_time is not None and recovery_budget_seconds > 0:
        components["recovery"] = max(0.0, min(1.0, 1.0 - recovery_time / recovery_budget_seconds))
        inputs["recovery"] = {"rule": "1 - recovery time after rollback / recovery budget",
                              "recovery_time_seconds": recovery_time, "budget_seconds": recovery_budget_seconds}
    health_values = [item.get("unrelated_component_healthy") for item in observations if "unrelated_component_healthy" in item]
    if health_values:
        components["containment"] = 1.0 if all(value is True for value in health_values) else 0.0
        inputs["containment"] = {"rule": "1 if every checked fault window kept unrelated components healthy, else 0",
                                 "windows_checked": len(health_values), "all_healthy": all(v is True for v in health_values)}
    if run.get("status") == "completed":
        components["detection"] = 1.0
        inputs["detection"] = {"rule": "1.0 for a completed run, 0.5 when the kill switch aborted it", "status": "completed"}
    elif run.get("abort_reason"):
        components["detection"] = 0.5
        inputs["detection"] = {"rule": "1.0 for a completed run, 0.5 when the kill switch aborted it",
                               "status": "aborted", "abort_reason": run["abort_reason"]}
    return components, inputs


def derive_score_components(run: dict[str, Any], recovery_budget_seconds: float = 180.0) -> dict[str, float]:
    """Derive score components from recorded fault observations without inventing data."""
    return score_breakdown(run, recovery_budget_seconds)[0]


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