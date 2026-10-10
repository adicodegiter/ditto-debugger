from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ai.findings import trace_check_markdown


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate that every numeric line in a report is tagged with a known run ID.")
    parser.add_argument("report", help="Markdown report path to validate.")
    parser.add_argument("--known-runs", nargs="*", default=[], help="Optional known run IDs for tag validation.")
    args = parser.parse_args()
    path = Path(args.report)
    text = path.read_text(encoding="utf-8")
    try:
        trace_check_markdown(text, known_run_ids=set(args.known_runs))
    except ValueError as exc:  # pragma: no cover - CLI contract
        print(f"trace_check: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"trace_check: OK for {path}")


if __name__ == "__main__":
    main()
