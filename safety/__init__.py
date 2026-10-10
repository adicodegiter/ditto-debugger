"""Deterministic safety controls for local experiment execution.

Only the operator process (``python -m safety.cli``) executes experiments; the API never does.
"""

from .kill_switch import AbortDecision, KillSwitch
from .policy import AbortThresholds, Policy, SafetyViolation
from .runner import ExperimentRunner, RunnerError
from .scoring import calculate_resilience_score, derive_score_components

__all__ = [
    "AbortDecision",
    "AbortThresholds",
    "ExperimentRunner",
    "KillSwitch",
    "Policy",
    "RunnerError",
    "SafetyViolation",
    "calculate_resilience_score",
    "derive_score_components",
]
