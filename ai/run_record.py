from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truth(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "passed", "pass"}:
            return True
        if normalized in {"false", "no", "failed", "fail"}:
            return False
    return None


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp()


def _elapsed_seconds(start: Any, end: Any) -> float | None:
    start_epoch, end_epoch = _epoch(start), _epoch(end)
    if start_epoch is None or end_epoch is None:
        return None
    return max(0.0, end_epoch - start_epoch)


def _sli_windows(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in raw.get("observations", []):
        if not isinstance(item, dict):
            continue
        if item.get("phase") == "fault":
            candidates = {
                "write": {"error_rate": _number(item.get("write_error_rate")), "p95_ms": _number(item.get("write_p95_ms"))},
                "search": {"error_rate": _number(item.get("search_error_rate")), "p95_ms": None},
                "freshness": {
                    "error_rate": 0.0 if _number(item.get("freshness_samples")) else None,
                    "p95_s": _number(item.get("freshness_p95_seconds")),
                },
            }
        elif item.get("kind") == "sli" and item.get("window", "during") == "during":
            name = item.get("sli") or item.get("name")
            if not name:
                continue
            candidates = {str(name): {
                "error_rate": _number(item.get("error_rate")),
                "p95_ms": _number(item.get("p95_ms")),
                "p95_s": _number(item.get("p95_s")),
            }}
        else:
            continue
        for name, measurement in candidates.items():
            if all(measurement.get(key) is None for key in ("error_rate", "p95_ms", "p95_s")):
                continue
            score = tuple(measurement.get(key) or 0.0 for key in ("error_rate", "p95_ms", "p95_s"))
            previous = result.get(name)
            previous_score = tuple((previous or {}).get(key) or 0.0 for key in ("error_rate", "p95_ms", "p95_s"))
            if previous is None or score > previous_score:
                result[name] = {
                    **measurement,
                    "window_start": item.get("window_start"),
                    "window_end": item.get("window_end"),
                }
    return result


def _search_outage_seconds(metrics_path: Path, timeline: dict[str, Any]) -> float | None:
    injected = _epoch(timeline.get("injected_at"))
    recovered = _epoch(timeline.get("recovered_at"))
    if injected is None or not metrics_path.is_file():
        return None
    failed_at: list[float] = []
    try:
        with metrics_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict) or item.get("kind") != "search" or item.get("success") is not False:
                    continue
                timestamp = _epoch(item.get("timestamp"))
                if timestamp is not None and timestamp >= injected and (recovered is None or timestamp <= recovered):
                    failed_at.append(timestamp)
    except OSError:
        return None
    if len(failed_at) < 2:
        return None
    return round(max(failed_at) - min(failed_at), 3)


def normalize_run_record(
    raw: dict[str, Any],
    *,
    experiment: dict[str, Any] | None = None,
    run_path: str | Path | None = None,
    metrics_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Map a safety run plus its immutable plan into the analysis record shape."""
    if not isinstance(raw, dict):
        raise ValueError("Run record must be a mapping.")
    if raw.get("_normalized_run_record") is True:
        return raw
    experiment = experiment or {}
    run_id = raw.get("run_id") or raw.get("id")
    if not run_id:
        raise ValueError("Missing required field: run_id")

    target = raw.get("target") or raw.get("target_component") or experiment.get("target_component")
    fault_type = (
        raw.get("fault_type") or raw.get("fault") or raw.get("action") or raw.get("operation")
        or experiment.get("action")
    )
    kind = str(raw.get("run_kind") or raw.get("kind") or "").strip().lower().replace("-", "_")
    forced_test = _truth(raw.get("forced_test")) is True or kind in {"forced_abort", "forced_test"}
    forced_test = forced_test or any(
        isinstance(item, dict) and item.get("kind") == "kill_switch" and _truth(item.get("forced_test")) is True
        for item in raw.get("observations", [])
    )
    dry_run = _truth(raw.get("dry_run")) is True or kind == "dry_run"
    if run_path is not None:
        dry_run = dry_run or "-dry-run" in Path(run_path).stem
        forced_test = forced_test or "-forced-test" in Path(run_path).stem

    verification = raw.get("verification") if isinstance(raw.get("verification"), dict) else {}
    recovery_marker = _truth(raw.get("recovery_verified"))
    if recovery_marker is None:
        recovery_marker = _truth(verification.get("passed"))
    legacy_recovery = next(
        (item for item in raw.get("observations", []) if isinstance(item, dict) and item.get("kind") == "recovery"),
        {},
    )
    if recovery_marker is None:
        recovery_marker = _truth(legacy_recovery.get("recovered"))
    recovery_state = "recovered" if recovery_marker is True else "not_recovered" if recovery_marker is False else "unknown"
    recovery_seconds = raw.get("recovery_time_seconds")
    if recovery_seconds is None:
        recovery_seconds = raw.get("recovery_seconds", raw.get("recovery_s"))
    if recovery_seconds is None:
        recovery_seconds = verification.get("seconds", verification.get("duration_seconds"))
    if recovery_seconds is None:
        recovery_seconds = legacy_recovery.get("recovery_s")

    timeline = raw.get("timeline") if isinstance(raw.get("timeline"), dict) else {}
    fault_elapsed = _number(raw.get("fault_elapsed_seconds"))
    if fault_elapsed is None:
        fault_elapsed = _elapsed_seconds(timeline.get("injected_at"), timeline.get("abort_at"))
    planned_duration = raw.get("planned_duration_s") or raw.get("duration_seconds")
    if planned_duration is None:
        params = raw.get("action_params")
        planned_duration = params.get("duration_seconds") if isinstance(params, dict) else None
    if planned_duration is None:
        planned_duration = experiment.get("duration_seconds")

    evidence_refs = list(raw.get("evidence_refs", []))
    plan_refs = experiment.get("evidence_refs", [])
    baseline_id = raw.get("baseline_id")
    if baseline_id is None:
        baseline_id = next(
            (ref.removeprefix("baseline:") for ref in plan_refs if isinstance(ref, str) and ref.startswith("baseline:")),
            None,
        )
    refs: list[str] = []
    if run_path is not None and Path(run_path).is_file():
        refs.append(str(Path(run_path)))
    if metrics_dir is not None:
        for name in ("write.jsonl", "search.jsonl", "freshness.jsonl"):
            path = Path(metrics_dir) / name
            if path.is_file():
                refs.append(str(path))
    refs.extend(ref for ref in [*evidence_refs, *plan_refs] if isinstance(ref, str) and ref not in refs)

    observations = raw.get("observations", [])
    fault_windows = [item for item in observations if isinstance(item, dict) and item.get("phase") == "fault"]
    write_error_rates = [_number(item.get("write_error_rate")) for item in fault_windows]
    write_p95_values = [_number(item.get("write_p95_ms")) for item in fault_windows]
    search_error_rates = [_number(item.get("search_error_rate")) for item in fault_windows]
    search_outage = _number(raw.get("search_outage_seconds"))
    if search_outage is None and metrics_dir is not None:
        search_outage = _search_outage_seconds(Path(metrics_dir) / "search.jsonl", timeline)

    status = raw.get("status")
    abort_reason = raw.get("abort_reason")
    aborted = _truth(raw.get("aborted"))
    if aborted is None:
        aborted = status == "aborted"
    kill_switch_tripped = bool(aborted and abort_reason)
    if not kill_switch_tripped:
        kill_switch_tripped = any(
            isinstance(item, dict) and item.get("kind") == "kill_switch" and _truth(item.get("tripped")) is True
            for item in observations
        )

    return {
        "_normalized_run_record": True,
        "run_id": str(run_id),
        "experiment_id": raw.get("experiment_id"),
        "fault_type": fault_type,
        "target": target,
        "planned_duration_s": _number(planned_duration),
        "fault_elapsed_seconds": fault_elapsed,
        "forced_test": forced_test,
        "dry_run": dry_run,
        "aborted": bool(aborted),
        "abort_reason": abort_reason,
        "kill_switch_tripped": kill_switch_tripped,
        "write_error_rate": max((value for value in write_error_rates if value is not None), default=None),
        "write_p95_ms": max((value for value in write_p95_values if value is not None), default=None),
        "search_error_rate": max((value for value in search_error_rates if value is not None), default=None),
        "search_outage_seconds": search_outage,
        "sli_windows": _sli_windows(raw),
        "recovery_state": recovery_state,
        "recovery_time_seconds": _number(recovery_seconds),
        "baseline_id": baseline_id,
        "evidence_refs": refs,
        "status": status,
        "cleanup_state": raw.get("cleanup_state"),
        "safety_violation": _truth(raw.get("safety_violation")) is True,
        "prediction": raw.get("prediction") if isinstance(raw.get("prediction"), dict) else {},
        "observations": observations,
        "timeline": timeline,
        "source": "runner_record" if "prediction" in raw and "cleanup_state" in raw else "legacy_fixture",
    }
