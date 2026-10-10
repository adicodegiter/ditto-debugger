#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from .sli import baseline_summary, load_thresholds
except ImportError:  # pragma: no cover - supports direct script execution
    from sli import baseline_summary, load_thresholds

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_METRICS_DIR = REPO_ROOT / "data" / "metrics"


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid timestamp {value!r}; use ISO-8601") from error
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Summarize the bounded Ditto workload baseline from JSON Lines metrics.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--metrics-dir", type=Path, default=DEFAULT_METRICS_DIR, help="directory containing write/search/freshness JSONL files")
    parser.add_argument("--run-id", help="summarize only records from this workload run")
    parser.add_argument("--from", dest="window_start", help="include samples at or after this ISO-8601 timestamp")
    parser.add_argument("--to", dest="window_end", help="include samples at or before this ISO-8601 timestamp")
    parser.add_argument("--thresholds-file", type=Path, default=REPO_ROOT / "configs" / "telemetry.yaml", help="YAML file containing SLI thresholds")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        window_start = _parse_timestamp(args.window_start)
        window_end = _parse_timestamp(args.window_end)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    if window_start and window_end and window_start > window_end:
        print("--from must be earlier than or equal to --to", file=sys.stderr)
        return 2
    summary = baseline_summary(
        args.metrics_dir,
        run_id=args.run_id,
        window_start=window_start,
        window_end=window_end,
        thresholds=load_thresholds(args.thresholds_file),
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
