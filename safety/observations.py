"""Kill-switch observations computed from real SLI samples (data/metrics/*.jsonl).

Accepts both record shapes used in this repo: CLAUDE.md's
``{"ts", "sli", "ok", "latency_ms"}`` and the workload's
``{"timestamp", "kind", "success", "latency_ms", "freshness_seconds"}``.
Nothing is filled in: a window without samples reports zero samples and
``telemetry_available: False``, which the kill switch treats as an abort.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Protocol


class ServiceStateSource(Protocol):
    def service_states(self) -> dict[str, dict[str, str]]: ...


def _parse_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _records(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record


def summarize_window(records: Iterable[dict[str, Any]], start: float, end: float) -> dict[str, Any]:
    write_ok: list[bool] = []
    write_latency: list[float] = []
    search_ok: list[bool] = []
    freshness: list[float] = []
    for record in records:
        ts = _parse_ts(record.get("timestamp", record.get("ts")))
        if ts is None or ts < start or ts > end:
            continue
        kind = str(record.get("kind", record.get("sli", ""))).strip().lower()
        ok = record.get("success", record.get("ok"))
        if kind == "write":
            write_ok.append(ok is True)
            latency = _number(record.get("latency_ms"))
            if latency is not None and ok is True:
                write_latency.append(latency)
        elif kind == "search":
            search_ok.append(ok is True)
        elif kind == "freshness":
            staleness = _number(record.get("freshness_seconds", record.get("staleness_s")))
            if staleness is not None:
                freshness.append(max(0.0, staleness))
    write_samples = len(write_ok)
    return {
        "write_samples": write_samples,
        "write_error_rate": (write_ok.count(False) / write_samples) if write_samples else None,
        "write_p95_ms": percentile(write_latency, 0.95),
        "search_samples": len(search_ok),
        "search_error_rate": (search_ok.count(False) / len(search_ok)) if search_ok else None,
        "freshness_samples": len(freshness),
        "freshness_p95_seconds": percentile(freshness, 0.95),
    }


def unrelated_services_healthy(states: dict[str, dict[str, str]], target: str) -> bool:
    """True only if every service except the target is running (and healthy where it has a healthcheck)."""
    others = {name: state for name, state in states.items() if name != target}
    if not others:
        return False
    for state in others.values():
        if state.get("state") != "running":
            return False
        if state.get("health") not in ("", "healthy"):
            return False
    return True


class SliWindowObservationProvider:
    def __init__(
        self,
        metrics_dir: Path,
        services: ServiceStateSource,
        window_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.metrics_dir = Path(metrics_dir)
        self.services = services
        self.window_seconds = window_seconds
        self.clock = clock

    def observe(self, target: str, since: float | None = None) -> dict[str, Any]:
        end = self.clock()
        start = end - self.window_seconds
        if since is not None:
            start = max(start, since)
        paths = [self.metrics_dir / name for name in ("write.jsonl", "search.jsonl", "freshness.jsonl")]
        summary = summarize_window(_records(paths), start, end)
        try:
            unrelated = unrelated_services_healthy(self.services.service_states(), target)
        except Exception:  # noqa: BLE001 - an unreadable state is unhealthy, never healthy
            unrelated = False
        summary.update({
            "source": "sli_jsonl",
            "freshness_method": "poll-proxy",
            "window_start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
            "window_end": datetime.fromtimestamp(end, timezone.utc).isoformat(),
            "telemetry_available": summary["write_samples"] > 0 and summary["freshness_samples"] > 0,
            "unrelated_component_healthy": unrelated,
        })
        return summary
