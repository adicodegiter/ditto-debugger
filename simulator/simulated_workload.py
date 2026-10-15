#!/usr/bin/env python3
"""Simulated fleet workload: healthy SLI samples without a Ditto stack.

This exists so a deployment with no reachable Ditto (e.g. the Render backend) can still exercise
the full control-plane flow -- samples -> baseline -> plan -> human approval -- end to end.

Honesty rules (do not weaken these):
  * Every record carries ``source: "simulated"`` and a run id prefixed ``sim-``. Live-sample
    collectors (``monitor/window.py``, ``monitor/sli.py``) skip them unless the operator has
    explicitly set ``WORKLOAD_MODE=simulated``.
  * A baseline captured from these samples is stamped ``sample_source: "simulated"`` with a note;
    it can never be mistaken for live telemetry.
  * This module NEVER injects a fault and never talks to Docker. Fault execution stays behind
    typed human approval in the operator CLI.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor.sli import append_jsonl, baseline_summary, load_thresholds, utc_timestamp  # noqa: E402

THRESHOLDS_PATH = Path(__file__).resolve().parents[1] / "configs" / "telemetry.yaml"

# Healthy but plausible probe latencies, comfortably inside the policy abort thresholds
# (write p95 multiplier 3x, search p95 <= 5000 ms, freshness p95 <= 30 s).
WRITE_MS = (8.0, 35.0)
SEARCH_MS = (10.0, 60.0)
FRESHNESS_S = (0.2, 2.5)


def simulated_records(*, run_id: str, at: datetime, vehicles: int, rng: random.Random) -> list[dict[str, Any]]:
    """One second's worth of healthy simulated probe records for every vehicle."""
    timestamp = utc_timestamp(at)
    records: list[dict[str, Any]] = []
    for index in range(1, vehicles + 1):
        vehicle_id = f"sim:fleet-vehicle-{index:02d}"
        common = {
            "source": "simulated",
            "run_id": run_id,
            "vehicle_id": vehicle_id,
            "timestamp": timestamp,
            "success": True,
        }
        records.append({"kind": "write", "latency_ms": round(rng.uniform(*WRITE_MS), 3), "status_code": 204, **common})
        records.append({"kind": "search", "latency_ms": round(rng.uniform(*SEARCH_MS), 3), "status_code": 200, "expected_hit": True, **common})
        records.append({"kind": "freshness", "freshness_seconds": round(rng.uniform(*FRESHNESS_S), 3), "method": "simulated", **common})
    return records


def write_tick(metrics_dir: Path, *, run_id: str, at: datetime, vehicles: int, rng: random.Random) -> int:
    records = simulated_records(run_id=run_id, at=at, vehicles=vehicles, rng=rng)
    files = {"write": "write.jsonl", "search": "search.jsonl", "freshness": "freshness.jsonl"}
    for record in records:
        append_jsonl(metrics_dir / files[record["kind"]], record)
    return len(records)


def backfill_simulated_samples(
    metrics_dir: Path | str,
    *,
    duration_seconds: float = 600.0,
    vehicles: int = 4,
    end_time: datetime | None = None,
    seed: int | None = 20261015,
    run_id: str | None = None,
) -> str:
    """Write ``duration_seconds`` of simulated samples ending at ``end_time`` (default: now).

    Used by the CLI for one-shot demos and by tests; writes timestamps in the past without
    sleeping. Returns the run id.
    """
    directory = Path(metrics_dir)
    directory.mkdir(parents=True, exist_ok=True)
    run = run_id or f"sim-{uuid.uuid4().hex[:12]}"
    rng = random.Random(seed)
    end = end_time or datetime.now(timezone.utc)
    start = end - timedelta(seconds=float(duration_seconds))
    tick = start
    while tick <= end:
        write_tick(directory, run_id=run, at=tick, vehicles=vehicles, rng=rng)
        tick += timedelta(seconds=1.0)
    return run


def run_simulated_workload(
    *,
    duration_seconds: float = 600.0,
    vehicles: int = 4,
    rate_per_second: float = 1.0,
    metrics_dir: Path | str = "data/metrics",
    seed: int | None = None,
    stop_event=None,
) -> dict[str, Any]:
    """Real-time simulated workload: writes one healthy tick per second until the deadline.

    Mirrors ``simulator.fleet_workload.run_fleet_workload``'s blocking shape so the backend's
    workload loop can use either interchangeably. Never touches a container.
    """
    directory = Path(metrics_dir)
    directory.mkdir(parents=True, exist_ok=True)
    run = f"sim-{uuid.uuid4().hex[:12]}"
    rng = random.Random(seed)
    tick_seconds = 1.0 / max(float(rate_per_second), 0.1)
    deadline = time.monotonic() + float(duration_seconds)
    written = 0
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            break
        written += write_tick(directory, run_id=run, at=datetime.now(timezone.utc), vehicles=vehicles, rng=rng)
        time.sleep(tick_seconds)

    summary = baseline_summary(directory, run_id=run, thresholds=load_thresholds(THRESHOLDS_PATH))
    summary["run"] = {
        "run_id": run,
        "mode": "simulated",
        "vehicles": vehicles,
        "duration_seconds": duration_seconds,
        "rate_per_second": rate_per_second,
        "samples_written": written,
        "captured_at": utc_timestamp(),
    }
    summary["note"] = "Simulated samples (source=simulated): no Ditto was contacted. Baselines from this run are stamped as simulated."
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Write healthy SIMULATED SLI samples (no Ditto needed).")
    parser.add_argument("--duration-seconds", type=float, default=600.0)
    parser.add_argument("--vehicles", type=int, default=4)
    parser.add_argument("--metrics-dir", default="data/metrics")
    args = parser.parse_args()
    run = backfill_simulated_samples(args.metrics_dir, duration_seconds=args.duration_seconds, vehicles=args.vehicles)
    print(json.dumps({"run_id": run, "mode": "simulated", "duration_seconds": args.duration_seconds, "metrics_dir": args.metrics_dir}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())