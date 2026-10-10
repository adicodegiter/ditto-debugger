from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

REQUIRED_KINDS = ("write", "search", "freshness")
DEFAULT_THRESHOLDS = {
    "write_error_rate_max": 0.20,
    "write_p95_multiplier": 3.0,
    "search_error_rate_max": 0.20,
    "search_p95_ms_max": 5000.0,
    "freshness_p95_seconds_max": 30.0,
}


def utc_timestamp(value: datetime | None = None) -> str:
    now = (value or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
        handle.write("\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
    return records


def percentile(values: Iterable[float], percent: float) -> float | None:
    items = sorted(float(value) for value in values if value is not None and math.isfinite(value))
    if not items:
        return None
    if percent <= 0:
        return items[0]
    if percent >= 1:
        return items[-1]
    index = max(0, min(len(items) - 1, math.ceil(percent * len(items)) - 1))
    return items[index]


def summarize_latencies(values: Iterable[float]) -> dict[str, float | int | None]:
    items = sorted(float(value) for value in values if value is not None and math.isfinite(value))
    if not items:
        return {"samples": 0, "p50": None, "p95": None, "mean": None}
    return {
        "samples": len(items),
        "p50": percentile(items, 0.50),
        "p95": percentile(items, 0.95),
        "mean": sum(items) / len(items),
    }


def load_thresholds(path: Path | str | None = None) -> dict[str, float]:
    thresholds = dict(DEFAULT_THRESHOLDS)
    if path is None or not Path(path).is_file():
        return thresholds
    with Path(path).open("r", encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    configured = config.get("thresholds", {})
    for key in thresholds:
        value = configured.get(key)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            thresholds[key] = float(value)
    return thresholds


def summarize_write_samples(samples: list[dict[str, Any]], thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    total = len(samples)
    successes = sum(1 for item in samples if bool(item.get("success")))
    latency_values = [float(item["latency_ms"]) for item in samples if isinstance(item.get("latency_ms"), (int, float))]
    latencies = summarize_latencies(latency_values)
    error_rate = 1.0 - (successes / total) if total else None
    return {
        "kind": "write",
        "samples": total,
        "success_rate": (successes / total) if total else 0.0,
        "error_rate": error_rate,
        "latency_ms": latencies,
        "status": "healthy" if total and error_rate is not None and error_rate <= limits["write_error_rate_max"] else "degraded",
    }


def summarize_search_samples(samples: list[dict[str, Any]], thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    total = len(samples)
    successes = sum(1 for item in samples if bool(item.get("success")))
    latency_values = [float(item["latency_ms"]) for item in samples if isinstance(item.get("latency_ms"), (int, float))]
    latencies = summarize_latencies(latency_values)
    expected_hits = sum(1 for item in samples if item.get("expected_hit") is True)
    misses = sum(1 for item in samples if item.get("expected_hit") is False)
    error_rate = 1.0 - (successes / total) if total else None
    return {
        "kind": "search",
        "samples": total,
        "success_rate": (successes / total) if total else 0.0,
        "error_rate": error_rate,
        "expected_hit_rate": (expected_hits / (expected_hits + misses)) if expected_hits + misses else None,
        "expected_hit_misses": misses,
        "latency_ms": latencies,
        "status": "healthy" if total and error_rate is not None and error_rate <= limits["search_error_rate_max"] and (latencies["p95"] is None or latencies["p95"] <= limits["search_p95_ms_max"]) else "degraded",
    }


def summarize_freshness_samples(samples: list[dict[str, Any]], now: datetime | None = None, thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    current = now or datetime.now(timezone.utc)
    freshness_values: list[float] = []
    for item in samples:
        recorded_freshness = item.get("freshness_seconds")
        if isinstance(recorded_freshness, (int, float)) and math.isfinite(float(recorded_freshness)):
            freshness_values.append(max(0.0, float(recorded_freshness)))
            continue
        observed_at = item.get("observed_at") or item.get("telemetry_timestamp")
        if not observed_at:
            continue
        try:
            ts = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        freshness_values.append(max(0.0, (current - ts.astimezone(timezone.utc)).total_seconds()))
    summary = summarize_latencies(freshness_values)
    status = "healthy" if freshness_values and (summary["p95"] is None or summary["p95"] <= limits["freshness_p95_seconds_max"]) else "degraded"
    if samples and not freshness_values:
        status = "unknown"
    return {
        "kind": "freshness",
        "samples": len(freshness_values),
        "p95_seconds": summary["p95"],
        "p50_seconds": summary["p50"],
        "mean_seconds": summary["mean"],
        "method": "poll-proxy",
        "status": status,
    }


def summarize_metrics(samples: Iterable[dict[str, Any]], now: datetime | None = None, thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    batched = list(samples)
    grouped: dict[str, list[dict[str, Any]]] = {kind: [] for kind in REQUIRED_KINDS}
    for item in batched:
        kind = str(item.get("kind") or item.get("metric_name") or "").strip().lower()
        if kind in grouped:
            grouped[kind].append(item)
    write = summarize_write_samples(grouped.get("write", []), thresholds)
    search = summarize_search_samples(grouped.get("search", []), thresholds)
    freshness = summarize_freshness_samples(grouped.get("freshness", []), now, thresholds)
    warnings: list[str] = []
    for kind, summary in (("write", write), ("search", search), ("freshness", freshness)):
        if summary["samples"] == 0:
            warnings.append(f"Missing {kind} samples in the current window.")
    return {
        "status": "healthy" if not warnings and write["status"] == "healthy" and search["status"] == "healthy" and freshness["status"] == "healthy" else "unhealthy",
        "warnings": warnings,
        "write": write,
        "search": search,
        "freshness": freshness,
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _collect_samples_from_dir(
    directory: Path,
    *,
    run_id: str | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    if not directory.exists():
        return samples
    for path in sorted(directory.glob("*.jsonl")):
        for item in load_jsonl(path):
            if item.get("source", "live") != "live":
                continue
            if run_id is not None and item.get("run_id") != run_id:
                continue
            timestamp = _parse_timestamp(item.get("timestamp"))
            if timestamp is None:
                continue
            if window_start is not None and timestamp < window_start:
                continue
            if window_end is not None and timestamp > window_end:
                continue
            samples.append(item)
    return samples


def baseline_summary(
    directory: Path | str,
    now: datetime | None = None,
    *,
    run_id: str | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    base = Path(directory)
    samples = _collect_samples_from_dir(base, run_id=run_id, window_start=window_start, window_end=window_end)
    summary = summarize_metrics(samples, now, thresholds)
    summary["run_id"] = run_id
    summary["window"] = {
        "start": window_start.isoformat().replace("+00:00", "Z") if window_start else None,
        "end": window_end.isoformat().replace("+00:00", "Z") if window_end else None,
    }
    if summary["status"] == "unhealthy":
        summary["warnings"] = summary["warnings"] or ["No healthy baseline could be established from the available metric samples."]
    return summary
