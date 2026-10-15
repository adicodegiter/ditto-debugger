from __future__ import annotations

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


MIN_BASELINE_SECONDS = 120.0  # CLAUDE.md M2: baseline of at least 120 s


def _latest_live_run(directory: Path, *, include_simulated: bool = False) -> tuple[str | None, datetime | None, datetime | None, str | None]:
    """The run_id of the most recent write sample, its first/last write times, and its source.

    Simulated samples (``source: "simulated"``) are invisible unless ``include_simulated`` is set,
    so the default baseline path can never quietly consume synthetic data.
    """
    allowed_sources = {"live", "simulated"} if include_simulated else {"live"}
    runs: dict[str, list[datetime]] = {}
    sources: dict[str, str] = {}
    for record in load_jsonl(directory / "write.jsonl"):
        source = str(record.get("source", "live"))
        if source not in allowed_sources or not record.get("run_id"):
            continue
        timestamp = _parse_timestamp(record.get("timestamp"))
        if timestamp is None:
            continue
        run_id = str(record["run_id"])
        sources.setdefault(run_id, source)
        span = runs.setdefault(run_id, [timestamp, timestamp])
        span[0] = min(span[0], timestamp)
        span[1] = max(span[1], timestamp)
    if not runs:
        return None, None, None, None
    run_id, (first, last) = max(runs.items(), key=lambda item: item[1][1])
    return run_id, first, last, sources[run_id]


def baseline_from_metrics(
    metrics_dir: Path | str,
    *,
    thresholds_file: Path | str | None = None,
    window_seconds: float = 300.0,
    min_seconds: float = MIN_BASELINE_SECONDS,
    allow_simulated: bool = False,
) -> dict[str, Any]:
    """Baseline from the samples of the most recent workload run.

    Uses only that run (never older runs or saved files), at most the last
    ``window_seconds`` of it, and requires at least ``min_seconds`` of samples.
    ``status`` is ``available`` or ``unavailable`` (the store's contract).

    ``allow_simulated`` accepts simulated samples (``WORKLOAD_MODE=simulated``); the returned
    baseline is then stamped ``sample_source: "simulated"`` with a note, never as live telemetry.
    """
    directory = Path(metrics_dir)
    now = datetime.now(timezone.utc)
    run_id, first, last, sample_source = _latest_live_run(directory, include_simulated=allow_simulated)
    warnings: list[str] = []
    summary: dict[str, Any] = {"status": "unhealthy", "warnings": []}
    window_start = window_end = None
    span = 0.0
    if run_id is None:
        warnings.append("No live workload samples found; start simulator/fleet_workload.py first.")
    else:
        window_end = last
        window_start = max(first, last - timedelta(seconds=window_seconds))
        span = (window_end - window_start).total_seconds()
        summary = baseline_summary(directory, now, run_id=run_id, window_start=window_start, window_end=window_end,
                                   thresholds=load_thresholds(thresholds_file), include_simulated=allow_simulated)
        warnings.extend(summary.get("warnings", []))
        if span < min_seconds:
            warnings.append(f"Baseline needs at least {min_seconds:.0f} s of samples; run {run_id} has {span:.0f} s so far.")
        if (now - last).total_seconds() > 60:
            warnings.append(f"The latest workload run ({run_id}) stopped writing {int((now - last).total_seconds())} s ago.")
    available = run_id is not None and summary["status"] == "healthy" and not warnings
    result: dict[str, Any] = {
        "baseline_id": f"client-baseline-{now.strftime('%Y%m%d%H%M%S')}",
        "captured_at": now.isoformat(),
        "status": "available" if available else "unavailable",
        "coverage": 1.0 if available else 0.0,
        "telemetry": summary,
        "source": "client-sli-jsonl",
        "run_id": run_id,
        "window": {
            "start": window_start.isoformat() if window_start else None,
            "end": window_end.isoformat() if window_end else None,
            "seconds": round(span, 1),
        },
        "warnings": warnings,
    }
    if sample_source == "simulated":
        result["sample_source"] = "simulated"
        result["note"] = "Captured from SIMULATED samples (WORKLOAD_MODE=simulated); not live Ditto telemetry."
    return result
