from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .sli import baseline_summary, load_jsonl, load_thresholds, summarize_metrics


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def window_stats(
    metrics_dir: Path | str,
    *,
    last_s: float = 30.0,
    now: datetime | None = None,
    baseline: dict[str, Any] | None = None,
    thresholds_file: Path | str | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = current - timedelta(seconds=last_s)
    samples: list[dict[str, Any]] = []
    directory = Path(metrics_dir)
    for path in sorted(directory.glob("*.jsonl")) if directory.exists() else []:
        for record in load_jsonl(path):
            if record.get("source", "live") != "live":
                continue
            timestamp = _parse_timestamp(record.get("timestamp"))
            if timestamp is not None and start <= timestamp <= current:
                samples.append(record)
    summary = summarize_metrics(samples, current, load_thresholds(thresholds_file))
    write_p95 = summary["write"]["latency_ms"]["p95"]
    baseline_p95 = None
    if baseline:
        baseline_p95 = (
            baseline.get("write", {}).get("latency_ms", {}).get("p95")
            or baseline.get("telemetry", {}).get("write", {}).get("latency_ms", {}).get("p95")
        )
    write_error_rate = summary["write"].get("error_rate")
    search_error_rate = summary["search"].get("error_rate")
    # The kill switch protects the write path. Search degradation remains
    # visible in the observation and report without aborting an expected
    # things-search experiment.
    error_rate = float(write_error_rate or 0.0)
    thresholds = load_thresholds(thresholds_file)
    p95_limit = None if baseline_p95 is None else float(baseline_p95) * thresholds["write_p95_multiplier"]
    missing = bool(summary["warnings"])
    return {
        "telemetry_available": not missing,
        "baseline_healthy": True,
        "unrelated_component_healthy": not missing,
        "error_rate": error_rate,
        "write_error_rate": write_error_rate,
        "search_error_rate": search_error_rate,
        "search_p95_latency": summary["search"]["latency_ms"]["p95"],
        "p95_latency": write_p95,
        "baseline_p95_latency": baseline_p95,
        "p95_latency_limit": p95_limit,
        "freshness_p95_seconds": summary["freshness"].get("p95_seconds"),
        "freshness_p95_limit": thresholds["freshness_p95_seconds_max"],
        "coverage": 0.0 if missing else 1.0,
        "summary": summary,
        "window": {"start": start.isoformat(), "end": current.isoformat()},
    }


def baseline_from_metrics(metrics_dir: Path | str, *, thresholds_file: Path | str | None = None) -> dict[str, Any]:
    metrics_path = Path(metrics_dir)
    artifacts = list((metrics_path.parent / "baselines").glob("baseline-*.json"))
    if artifacts:
        latest = max(artifacts, key=lambda path: path.stat().st_mtime)
        try:
            artifact = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            artifact = None
        if isinstance(artifact, dict) and isinstance(artifact.get("summary"), dict):
            return {
                "baseline_id": artifact.get("baseline_id"),
                "captured_at": artifact.get("captured_at"),
                "status": artifact.get("status"),
                "coverage": 1.0 if artifact.get("status") == "healthy" else 0.0,
                "telemetry": artifact["summary"],
                "source": artifact.get("source", "client-sli-jsonl"),
                "run_id": artifact.get("run_id"),
            }
    summary = baseline_summary(metrics_dir, thresholds=load_thresholds(thresholds_file))
    return {
        "baseline_id": f"client-baseline-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "status": "available" if summary["status"] == "healthy" else "unavailable",
        "coverage": 1.0 if summary["status"] == "healthy" else 0.0,
        "telemetry": summary,
        "source": "client-sli-jsonl",
    }
