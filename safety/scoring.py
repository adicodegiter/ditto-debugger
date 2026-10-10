from __future__ import annotations

from typing import Any
import math


WEIGHTS = {"availability": 0.30, "latency": 0.20, "recovery": 0.20, "containment": 0.15, "detection": 0.15}


def calculate_resilience_score(run: dict[str, Any]) -> dict[str, Any]:
    if run.get("safety_violation"):
        return {"status": "unsafe", "score": None, "coverage": 0.0, "components": {}}
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