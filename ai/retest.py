from __future__ import annotations

from typing import Any


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _reported(run: dict[str, Any]) -> dict[str, Any]:
    value = run.get("_reported_results")
    return value if isinstance(value, dict) else {}


def _first_number(run: dict[str, Any], names: tuple[str, ...], *, observations: bool = False) -> float | None:
    reported = _reported(run)
    for source in (run, reported):
        for name in names:
            result = _as_number(source.get(name))
            if result is not None:
                return result
    if observations:
        values = [
            value
            for item in run.get("observations", [])
            if isinstance(item, dict) and item.get("phase") == "fault"
            for name in names
            if (value := _as_number(item.get(name))) is not None
        ]
        if values:
            return max(values)
    return None


def _run_facts(run: dict[str, Any]) -> dict[str, Any]:
    reported = _reported(run)
    action_params = run.get("action_params") if isinstance(run.get("action_params"), dict) else {}
    diagnosis = run.get("diagnosis") if isinstance(run.get("diagnosis"), dict) else {}
    action = run.get("action") or run.get("fault_type") or reported.get("action") or diagnosis.get("action")
    target = run.get("target") or run.get("target_component") or reported.get("target") or diagnosis.get("target_component")
    duration = run.get("planned_duration_s") or action_params.get("duration_seconds") or reported.get("duration_seconds")
    baseline_write_p95 = _first_number(run, ("baseline_write_p95_ms",))
    return {
        "run_id": run.get("run_id"),
        "action": action,
        "target": target,
        "duration_seconds": _as_number(duration),
        "baseline_id": run.get("baseline_id"),
        "reported_baseline_id": reported.get("baseline_id"),
        "baseline_write_p95_ms": baseline_write_p95,
        "client_timeout_seconds": _first_number(run, ("client_timeout_seconds", "search_timeout_seconds")),
    }


def _measurement(before: float | None, after: float | None, unit: str) -> dict[str, Any]:
    if before is None or after is None:
        return {"before": before, "after": after, "delta": None, "unit": unit, "conclusion": "not demonstrated"}
    return {
        "before": before,
        "after": after,
        "delta": round(after - before, 3),
        "unit": unit,
        "conclusion": "measured",
    }


def _search_timeouts(run: dict[str, Any]) -> dict[str, Any] | None:
    value = run.get("search_request_timeouts") or _reported(run).get("search_request_timeouts")
    return value if isinstance(value, dict) else None


def compare_retest_runs(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Compare a remediation pair without substituting for missing run evidence."""
    before_facts = _run_facts(before)
    after_facts = _run_facts(after)
    before_observations = [
        item for item in before.get("observations", [])
        if isinstance(item, dict) and item.get("phase") == "fault"
    ]
    after_observations = [
        item for item in after.get("observations", [])
        if isinstance(item, dict) and item.get("phase") == "fault"
    ]

    def report_measurement(run: dict[str, Any], observations: list[dict[str, Any]], keys: tuple[str, ...]) -> float | None:
        value = _first_number(run, keys)
        if value is not None:
            return value
        values = [
            measured
            for item in observations
            for key in keys
            if (measured := _as_number(item.get(key))) is not None
        ]
        return max(values) if values else None

    before_metrics = _reported(before)
    after_metrics = _reported(after)
    before_outage = _first_number(before, ("search_outage_seconds", "search_outage_seconds_approx"))
    after_outage = _first_number(after, ("search_outage_seconds", "search_outage_seconds_approx"))
    before_failure_wait = _first_number(before, ("search_failure_wait_seconds", "search_hang_seconds"))
    after_failure_wait = _first_number(after, ("search_failure_wait_seconds", "search_hang_seconds"))
    before_search_p95 = report_measurement(before, before_observations, ("search_p95_ms", "search_latency_p95_ms"))
    after_search_p95 = report_measurement(after, after_observations, ("search_p95_ms", "search_latency_p95_ms"))
    before_write_error = report_measurement(before, before_observations, ("write_error_rate",))
    after_write_error = report_measurement(after, after_observations, ("write_error_rate",))
    before_write_p95 = report_measurement(before, before_observations, ("write_p95_ms", "write_p95_ms_upper_bound"))
    after_write_p95 = report_measurement(after, after_observations, ("write_p95_ms", "write_p95_ms_upper_bound"))
    before_recovery = _first_number(before, ("recovery_time_seconds", "recovery_s"))
    after_recovery = _first_number(after, ("recovery_time_seconds", "recovery_s"))

    timeout_before = _search_timeouts(before)
    timeout_after = _search_timeouts(after)
    timeout_delta = None
    if timeout_before is not None and timeout_after is not None:
        count_before = _as_number(timeout_before.get("count"))
        count_after = _as_number(timeout_after.get("count"))
        if count_before is not None and count_after is not None:
            timeout_delta = round(count_after - count_before, 3)

    baseline_ids = {
        "before": before_facts["baseline_id"],
        "after": after_facts["baseline_id"],
        "after_reported_in_summary": after_facts["reported_baseline_id"],
    }
    conditions = {
        "same_fault": before_facts["action"] == after_facts["action"] if before_facts["action"] and after_facts["action"] else None,
        "same_target": before_facts["target"] == after_facts["target"] if before_facts["target"] and after_facts["target"] else None,
        "same_duration": before_facts["duration_seconds"] == after_facts["duration_seconds"] if before_facts["duration_seconds"] is not None and after_facts["duration_seconds"] is not None else None,
        "baseline_ids": baseline_ids,
        "baseline_write_p95_ms": _measurement(
            before_facts["baseline_write_p95_ms"], after_facts["baseline_write_p95_ms"], "ms"
        ),
        "client_timeout_seconds": _measurement(
            before_facts["client_timeout_seconds"], after_facts["client_timeout_seconds"], "s"
        ),
    }

    return {
        "before_run_id": before_facts["run_id"],
        "after_run_id": after_facts["run_id"],
        "conditions": conditions,
        "measurements": {
            "search_outage_seconds": _measurement(before_outage, after_outage, "s"),
            "last_search_failure_after_rollback_seconds": _measurement(
                _first_number(before, ("last_search_failure_after_rollback_seconds",)),
                _first_number(after, ("last_search_failure_after_rollback_seconds",)),
                "s",
            ),
            "search_request_timeouts": {
                "before": timeout_before,
                "after": timeout_after,
                "delta_count": timeout_delta,
                "conclusion": "not demonstrated" if timeout_before is None or timeout_after is None else "counts recorded; windows may differ",
            },
            "search_failure_wait_seconds": _measurement(before_failure_wait, after_failure_wait, "s"),
            "search_latency_p95_ms": _measurement(before_search_p95, after_search_p95, "ms"),
            "write_error_rate": _measurement(before_write_error, after_write_error, "ratio"),
            "write_p95_ms": _measurement(before_write_p95, after_write_p95, "ms"),
            "recovery_time_seconds": _measurement(before_recovery, after_recovery, "s"),
            "write_samples_per_second": _measurement(
                _first_number(before, ("write_samples_per_second",)),
                _first_number(after, ("write_samples_per_second",)),
                "per s",
            ),
        },
        "overall_improvement": _overall_improvement(before, after, before_failure_wait, after_failure_wait),
    }


def _overall_improvement(before: dict[str, Any], after: dict[str, Any], wait_before: float | None, wait_after: float | None) -> str:
    """Client-side and server-side effects are judged separately; missing evidence is never an improvement."""
    if wait_before is None or wait_after is None:
        return "not demonstrated"
    rate_before = _first_number(before, ("write_samples_per_second",))
    rate_after = _first_number(after, ("write_samples_per_second",))
    if rate_before is None or rate_after is None:
        return "not demonstrated"
    client_side = wait_after < wait_before and rate_after >= rate_before
    errors_after = _reported(after).get("search_failure_errors") or after.get("search_failure_errors") or []
    still_timeouts = bool(errors_after) and all("Timeout" in str(error) for error in errors_after)
    if client_side and still_timeouts:
        return "client-side improvement demonstrated; server-side fail-fast not demonstrated"
    if client_side:
        return "client-side improvement demonstrated"
    return "not demonstrated"