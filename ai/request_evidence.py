"""Request-level evidence for one run: the raw probe samples around its fault window.

Run records hold 30 s SLI windows. Questions like "how long did a failing search hang" or
"how long was search out" need the individual probe samples from data/metrics/*.jsonl.
This module cuts the samples for one run's window (injection - pad to recovery + pad) into a
small JSON Lines file that can be committed next to the run record, and computes facts from it.

Deterministic, read-only: no execution, no network.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
KINDS = ("write", "search")
PAD_SECONDS = 5.0


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fault_window(run: dict[str, Any], pad_seconds: float = PAD_SECONDS) -> tuple[datetime, datetime] | None:
    timeline = run.get("timeline") or {}
    start, end = timeline.get("injected_at"), timeline.get("recovered_at") or timeline.get("rollback_at")
    if not start or not end:
        return None
    pad = timedelta(seconds=pad_seconds)
    return _ts(start) - pad, _ts(end) + pad


def extract_samples(run: dict[str, Any], metrics_dir: str | Path, pad_seconds: float = PAD_SECONDS) -> list[dict[str, Any]]:
    """Write and search probe samples whose completion time falls inside the run's padded window."""
    window = fault_window(run, pad_seconds)
    if window is None:
        return []
    low, high = window
    samples: list[dict[str, Any]] = []
    for kind in KINDS:
        path = Path(metrics_dir) / f"{kind}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            stamp = record.get("timestamp")
            if stamp and low <= _ts(stamp) <= high:
                samples.append({**record, "kind": record.get("kind", kind)})
    samples.sort(key=lambda item: item["timestamp"])
    return samples


def load_samples(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        return []
    return [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]


def request_facts(run: dict[str, Any], samples: list[dict[str, Any]], pad_seconds: float = PAD_SECONDS) -> dict[str, Any]:
    """Facts measured from individual probe samples. Missing evidence stays None, never guessed."""
    window = fault_window(run, pad_seconds)
    if window is None or not samples:
        return {}
    window_seconds = round((window[1] - window[0]).total_seconds(), 1)
    search = [s for s in samples if s.get("kind") == "search"]
    writes = [s for s in samples if s.get("kind") == "write"]
    failed = [s for s in search if not s.get("success")]
    facts: dict[str, Any] = {
        "window_seconds": window_seconds,
        "search_samples": len(search),
        "search_failures": len(failed),
        "search_failure_errors": sorted({str(s.get("error") or s.get("status_code")) for s in failed}),
        "write_samples": len(writes),
        "write_failures": sum(1 for s in writes if not s.get("success")),
        "write_samples_per_second": round(len(writes) / window_seconds, 2) if window_seconds else None,
        "search_request_timeouts": {"count": len(failed), "failures": len(failed), "window_seconds": window_seconds},
        "search_failure_wait_seconds": None,
        "search_outage_seconds": None,
        "last_search_failure_after_rollback_seconds": None,
    }
    if failed:
        waits = [float(s.get("latency_ms") or 0.0) / 1000.0 for s in failed]
        facts["search_failure_wait_seconds"] = round(max(waits), 1)
        # A failed request started latency_ms before its completion timestamp.
        first_start = min(_ts(s["timestamp"]) - timedelta(milliseconds=float(s.get("latency_ms") or 0.0)) for s in failed)
        last_end = max(_ts(s["timestamp"]) for s in failed)
        facts["search_outage_seconds"] = round((last_end - first_start).total_seconds(), 1)
        rollback = (run.get("timeline") or {}).get("rollback_at")
        if rollback:
            facts["last_search_failure_after_rollback_seconds"] = round((last_end - _ts(rollback)).total_seconds(), 1)
    return facts


def main() -> None:
    parser = argparse.ArgumentParser(description="Cut one run's raw probe samples (write, search) out of data/metrics into a small JSON Lines file.")
    parser.add_argument("--run", required=True, type=Path, help="run record JSON")
    parser.add_argument("--metrics-dir", type=Path, default=ROOT / "data" / "metrics", help="directory with write.jsonl and search.jsonl")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs" / "evidence" / "windows-host" / "samples")
    parser.add_argument("--pad-seconds", type=float, default=PAD_SECONDS)
    args = parser.parse_args()
    run = json.loads(args.run.read_text(encoding="utf-8"))
    samples = extract_samples(run, args.metrics_dir, args.pad_seconds)
    if not samples:
        raise SystemExit(f"No samples in {args.metrics_dir} for {run.get('run_id')}'s window; nothing written.")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"{run['run_id']}.jsonl"
    out.write_text("".join(json.dumps(s, sort_keys=True) + "\n" for s in samples), encoding="utf-8", newline="\n")
    print(f"Saved {len(samples)} samples to {out}")
    print(json.dumps(request_facts(run, samples, args.pad_seconds), indent=2))


if __name__ == "__main__":
    main()
