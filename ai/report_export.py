from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ai.findings import build_report_markdown

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = ROOT / "docs" / "findings-report.md"


def _load_runs(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if source.is_dir():
        records: list[dict[str, Any]] = []
        for child in sorted(source.iterdir()):
            if child.is_file() and child.suffix.lower() in {".json", ".jsonl"}:
                records.extend(_load_runs(child))
        return records
    if source.suffix.lower() == ".jsonl":
        payload: list[dict[str, Any]] = []
        for line in source.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            payload.append(json.loads(text))
        return payload
    data = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the findings report from run JSON fixtures or a record directory.")
    parser.add_argument("--runs", default=str(ROOT / "tests" / "fixtures"), help="Path to a JSON file, JSONL file, or directory of run records.")
    parser.add_argument("--out", default=str(DEFAULT_REPORT_PATH), help="Destination Markdown report path.")
    parser.add_argument("--policy", default=str(ROOT / "configs" / "experiment-policy.yaml"), help="Experiment policy used for report context.")
    args = parser.parse_args()
    runs = _load_runs(args.runs)
    report = build_report_markdown(runs, policy_path=args.policy, output_path=args.out)
    print(f"Saved findings report to {args.out}")
    print(report.splitlines()[0])


if __name__ == "__main__":
    main()
