"""Read-only views for the dashboard: live SLIs, policy, dependency map, watchdog log.

Everything here only reads local files (data/metrics, configs, ai/, data/experiments).
Nothing here executes or changes anything.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from typing import Any

from safety.observations import _records, percentile, summarize_window
from safety.policy import Policy

METRIC_FILES = ("write.jsonl", "search.jsonl", "freshness.jsonl")


def _parse(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _tail_records(metrics_dir: Path, since: float) -> list[dict[str, Any]]:
    """Records newer than ``since`` (reads the files; fine for hackathon-sized JSONL)."""
    paths = [Path(metrics_dir) / name for name in METRIC_FILES]
    output = []
    for record in _records(paths):
        ts = _parse(record.get("timestamp", record.get("ts")))
        if ts is not None and ts >= since:
            output.append(record)
    return output


def sli_window(metrics_dir: Path, window_seconds: float = 30.0, now: float | None = None) -> dict[str, Any]:
    end = now or time.time()
    start = end - window_seconds
    records = _tail_records(metrics_dir, start)
    summary = summarize_window(records, start, end)
    latest = max((_parse(r.get("timestamp", r.get("ts"))) or 0 for r in records), default=None)
    methods = sorted({str(r.get("method")) for r in records if r.get("kind") == "freshness" and r.get("method")})
    return {
        **summary,
        "window_seconds": window_seconds,
        "window_start": _iso(start),
        "window_end": _iso(end),
        "latest_sample_at": _iso(latest) if latest else None,
        "freshness_method": methods[0] if len(methods) == 1 else ("mixed" if methods else None),
        "live": bool(latest and end - latest < 15),
    }


def sli_series(metrics_dir: Path, minutes: float = 5.0, bucket_seconds: float = 10.0, now: float | None = None) -> dict[str, Any]:
    end = now or time.time()
    start = end - minutes * 60
    records = _tail_records(metrics_dir, start)
    buckets = max(1, int(math.ceil((end - start) / bucket_seconds)))
    series: list[dict[str, Any]] = []
    grouped: list[list[dict[str, Any]]] = [[] for _ in range(buckets)]
    for record in records:
        ts = _parse(record.get("timestamp", record.get("ts")))
        if ts is None:
            continue
        index = min(buckets - 1, int((ts - start) // bucket_seconds))
        if index >= 0:
            grouped[index].append(record)
    for index, items in enumerate(grouped):
        bucket_start = start + index * bucket_seconds
        summary = summarize_window(items, bucket_start, bucket_start + bucket_seconds) if items else None
        series.append({
            "t": _iso(bucket_start),
            "write_samples": summary["write_samples"] if summary else 0,
            "write_success": (1 - summary["write_error_rate"]) if summary and summary["write_error_rate"] is not None else None,
            "write_p95_ms": summary["write_p95_ms"] if summary else None,
            "search_success": (1 - summary["search_error_rate"]) if summary and summary["search_error_rate"] is not None else None,
            "freshness_p95_seconds": summary["freshness_p95_seconds"] if summary else None,
        })
    return {"minutes": minutes, "bucket_seconds": bucket_seconds, "points": series}


def policy_summary(policy_path: Path, execution_enabled: bool) -> dict[str, Any]:
    policy = Policy.from_file(policy_path)
    thresholds = policy.thresholds
    return {
        "compose_project": policy.compose_project,
        "allowed_targets": sorted(policy.allowed_targets),
        "denied_targets": sorted(policy.denied_targets),
        "enabled_actions": sorted(policy.enabled_actions),
        "max_duration_seconds": policy.max_duration_seconds,
        "max_risk_score": policy.max_risk_score,
        "approval_required": policy.approval_required,
        "approval_expires_after_minutes": policy.approval_expires_after_minutes,
        "baseline_max_age_seconds": policy.baseline_max_age_seconds,
        "abort_thresholds": {
            "window_seconds": thresholds.window_seconds,
            "poll_interval_seconds": thresholds.poll_interval_seconds,
            "write_error_rate": thresholds.write_error_rate,
            "write_p95_multiplier": thresholds.write_p95_multiplier,
            "freshness_p95_seconds": thresholds.freshness_p95_seconds,
            "min_write_samples": thresholds.min_write_samples,
            "min_freshness_samples": thresholds.min_freshness_samples,
        },
        "recovery_timeout_seconds": policy.recovery_timeout_seconds,
        "watchdog_grace_seconds": policy.watchdog_grace_seconds,
        "operator_execution_enabled": execution_enabled,
        "api_executes": False,
    }


def dependency_map(path: Path) -> dict[str, Any]:
    if not Path(path).is_file():
        return {"status": "unavailable", "nodes": [], "edges": [], "warnings": ["Run python -m ai.dependency_map to generate it."]}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def watchdog_log(path: Path, limit: int = 50) -> list[dict[str, Any]]:
    if not Path(path).is_file():
        return []
    lines = Path(path).read_text(encoding="utf-8").splitlines()[-limit:]
    events = []
    for line in reversed(lines):
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def baseline_view(baseline: dict[str, Any]) -> dict[str, Any]:
    telemetry = baseline.get("telemetry") or {}
    summary = telemetry.get("telemetry") or {}
    write = summary.get("write") or {}
    search = summary.get("search") or {}
    freshness = summary.get("freshness") or {}
    return {
        "baseline_id": baseline.get("baseline_id"),
        "captured_at": baseline.get("captured_at"),
        "graph_version": baseline.get("graph_version"),
        "telemetry_status": baseline.get("telemetry_status"),
        "run_id": telemetry.get("run_id"),
        "window": telemetry.get("window"),
        "write_samples": write.get("samples"),
        "write_error_rate": write.get("error_rate"),
        "write_p95_ms": (write.get("latency_ms") or {}).get("p95"),
        "search_error_rate": search.get("error_rate"),
        "freshness_p95_seconds": freshness.get("p95_seconds"),
        "freshness_method": freshness.get("method"),
    }


def percentile_of(values: list[float], fraction: float) -> float | None:
    return percentile(values, fraction)
