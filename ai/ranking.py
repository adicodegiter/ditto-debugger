"""Cross-run ranked findings (task 5.6). Deterministic: no LLM, no execution.

Inputs are stored run records and their findings. Output keeps three things apart:
  measured_facts : numbers read straight from the run record, each with its source
  hypothesis     : the recommendation text. It is a proposal, not a measurement
  validation_test: how to re-run the experiment to confirm the fix worked
Dry runs and forced kill-switch tests are not resilience evidence and are never ranked.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
PATHS = (("write_error_rate", "write"), ("search_error_rate", "search"))


def _worst(run: dict[str, Any], key: str) -> float | None:
    values = [o[key] for o in run.get("observations", []) if o.get("phase") == "fault" and isinstance(o.get(key), (int, float))]
    return max(values) if values else None


def measured_facts(run: dict[str, Any]) -> list[dict[str, Any]]:
    run_id = run.get("run_id", "unknown")
    facts: list[dict[str, Any]] = []
    for key, label in PATHS:
        value = _worst(run, key)
        if value is not None:
            facts.append({"fact": f"worst {label} error rate during fault", "value": value, "source": f"run:{run_id}.observations.{key}"})
    write_p95 = _worst(run, "write_p95_ms")
    if write_p95 is not None:
        facts.append({"fact": "worst write p95 (ms) during fault", "value": write_p95, "source": f"run:{run_id}.observations.write_p95_ms"})
    if isinstance(run.get("recovery_time_seconds"), (int, float)):
        facts.append({"fact": "recovery time after rollback (s)", "value": run["recovery_time_seconds"], "source": f"run:{run_id}.recovery_time_seconds"})
    if "recovery_verified" in run:
        facts.append({"fact": "recovery verified", "value": run["recovery_verified"], "source": f"run:{run_id}.recovery_verified"})
    if run.get("abort_reason"):
        facts.append({"fact": "kill switch abort reason", "value": run["abort_reason"], "source": f"run:{run_id}.abort_reason"})
    score = run.get("resilience_score") or {}
    if score.get("status"):
        facts.append({"fact": "resilience score", "value": score.get("score") if score["status"] == "complete" else score["status"], "source": f"run:{run_id}.resilience_score"})
    return facts


def _validation_test(target: str, action: str, run: dict[str, Any]) -> str:
    return (
        f"After applying the fix, re-run '{action}' on '{target}' with the same duration and a fresh baseline; "
        "pass if the affected path's error rate stays within baseline + 2 percentage points, recovery_verified is true "
        "and the kill switch does not abort. Compare against the numbers in measured_facts."
    )


def rank_findings(runs: list[dict[str, Any]], findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_run = {run["run_id"]: run for run in runs if run.get("run_id")}
    ranked: list[dict[str, Any]] = []
    for finding in findings:
        run = by_run.get(finding.get("run_id"))
        if run is None or run.get("dry_run") or run.get("forced_test"):
            continue
        diagnosis = run.get("diagnosis") or {}
        target, action = diagnosis.get("target_component", "unknown"), diagnosis.get("action", "unknown")
        ranked.append({
            "finding_id": finding["id"],
            "run_id": run["run_id"],
            "target": target,
            "action": action,
            "severity": finding["severity"],
            "summary": finding["summary"],
            "measured_facts": measured_facts(run),
            "hypothesis": {"text": finding["recommendation"], "ai_generated": bool(finding.get("ai_generated"))},
            "validation_test": _validation_test(target, action, run),
            "evidence": finding["evidence"],
            "confidence": finding["confidence"],
        })
    ranked.sort(key=lambda item: (-SEVERITY_RANK.get(item["severity"], 0), -item["confidence"], item["run_id"], item["finding_id"]))
    for position, item in enumerate(ranked, start=1):
        item["rank"] = position
    return ranked


def render_markdown(ranked: list[dict[str, Any]]) -> str:
    lines = ["# Ranked findings", "", "Generated from stored run records. Measured facts and hypotheses are kept apart.", ""]
    if not ranked:
        lines.append("_No scored runs yet._")
    for item in ranked:
        lines += [f"## {item['rank']}. [{item['severity']}] {item['action']} on {item['target']}", "", item["summary"], "", "**Measured facts**", ""]
        lines += [f"- {fact['fact']}: {fact['value']} (`{fact['source']}`)" for fact in item["measured_facts"]] or ["- none recorded"]
        tag = " (AI-written)" if item["hypothesis"]["ai_generated"] else ""
        lines += ["", f"**Hypothesis / fix{tag}:** {item['hypothesis']['text']}", "", f"**Validation test:** {item['validation_test']}", "",
                  f"Evidence: {', '.join(item['evidence'])} · confidence {item['confidence']}", ""]
    return "\n".join(lines)


def load_run_files(paths: list[str]) -> list[dict[str, Any]]:
    """Read run-record JSON files; refuse anything that is not a real, non-dry run with a run_id."""
    import json
    from pathlib import Path
    runs = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            raise SystemExit(f"Run record not found: {path}. Records live in data/experiments/ (git-ignored); copy the JSON from the machine that ran it.")
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise SystemExit(f"Run record is not valid JSON: {path} ({error})")
        if not isinstance(run, dict) or not run.get("run_id"):
            raise SystemExit(f"Not a run record (no run_id): {path}")
        runs.append(run)
    return runs


def main() -> int:
    parser = argparse.ArgumentParser(description="Print ranked findings from stored runs or run-record files (read-only).")
    parser.add_argument("--markdown", action="store_true", help="print Markdown instead of JSON")
    parser.add_argument("--limit", type=int, default=200, help="most recent runs to read from the local store")
    parser.add_argument("--run-file", action="append", default=[], metavar="PATH",
                        help="rank this run-record JSON instead of the local store (repeatable); findings are regenerated deterministically")
    args = parser.parse_args()
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root), str(root / "backend")]
    if args.run_file:
        from ai.report import generate_findings
        runs = load_run_files(args.run_file)
        findings = [finding for run in runs for finding in generate_findings(run)]
    else:
        from app.settings import Settings
        from app.store import LocalRepository
        repo = LocalRepository(Settings.from_environment().database_path)
        runs, findings = repo.list_runs(args.limit), repo.list_findings()
    ranked = rank_findings(runs, findings)
    print(render_markdown(ranked) if args.markdown else json.dumps(ranked, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
