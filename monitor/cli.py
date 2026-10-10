#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .sli import baseline_summary, load_thresholds
except ImportError:  # pragma: no cover - supports direct script execution
    from sli import baseline_summary, load_thresholds

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_METRICS_DIR = REPO_ROOT / "data" / "metrics"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Summarize the bounded Ditto workload baseline from JSON Lines metrics.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--metrics-dir", type=Path, default=DEFAULT_METRICS_DIR, help="directory containing write/search/freshness JSONL files")
    parser.add_argument("--run-id", help="summarize only records from this workload run")
    parser.add_argument("--thresholds-file", type=Path, default=REPO_ROOT / "configs" / "telemetry.yaml", help="YAML file containing SLI thresholds")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    summary = baseline_summary(args.metrics_dir, run_id=args.run_id, thresholds=load_thresholds(args.thresholds_file))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
