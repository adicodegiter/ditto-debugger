#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

try:
    from .sli import append_jsonl, baseline_summary, utc_timestamp
except ImportError:  # pragma: no cover - supports direct script execution
    from sli import append_jsonl, baseline_summary, utc_timestamp

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_METRICS_DIR = REPO_ROOT / "data" / "metrics"


def _write_example_samples(metrics_dir: Path) -> None:
    metrics_dir.mkdir(parents=True, exist_ok=True)
    base = datetime.now(timezone.utc)
    for idx in range(10):
        append_jsonl(
            metrics_dir / "write.jsonl",
            {
                "kind": "write",
                "success": True,
                "latency_ms": 180 + idx * 7,
                "timestamp": utc_timestamp(base),
            },
        )
        append_jsonl(
            metrics_dir / "search.jsonl",
            {
                "kind": "search",
                "success": True,
                "latency_ms": 240 + idx * 9,
                "timestamp": utc_timestamp(base),
            },
        )
        append_jsonl(
            metrics_dir / "freshness.jsonl",
            {
                "kind": "freshness",
                "observed_at": utc_timestamp(base),
                "freshness_seconds": max(0.0, 3.0 + idx * 0.5),
                "timestamp": utc_timestamp(base),
            },
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Summarize the bounded Ditto workload baseline from JSON Lines metrics.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--metrics-dir", type=Path, default=DEFAULT_METRICS_DIR, help="directory containing write/search/freshness JSONL files")
    parser.add_argument("--seed-demo", action="store_true", help="write demo samples before summarizing")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.seed_demo:
        _write_example_samples(args.metrics_dir)
    summary = baseline_summary(args.metrics_dir)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
