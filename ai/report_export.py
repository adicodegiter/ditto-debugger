from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from ai.findings import trace_check_markdown
from ai.retest import compare_retest_runs

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = ROOT / "docs" / "findings-report.md"
DEFAULT_RUNS_PATH = ROOT / "data" / "experiments"
DEFAULT_DIAGNOSES_PATH = ROOT / "data" / "diagnoses"
DEFAULT_FINDINGS_PATH = ROOT / "data" / "findings-ranked.json"
DEFAULT_RETEST_BEFORE_PATH = ROOT / "tests" / "fixtures" / "restart-retest-before.json"
DEFAULT_RETEST_EVIDENCE_PATH = ROOT / "tests" / "fixtures" / "restart-retest-after.json"
DEFAULT_FORCED_FIXTURE_PATH = ROOT / "tests" / "fixtures" / "m4-forced-test-docs-reconstruction.json"


def _load_runs(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if source.is_dir():
        records: list[dict[str, Any]] = []
        for child in sorted(source.iterdir()):
            if child.is_file() and child.name.endswith(".verify.json"):
                continue
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


def _load_diagnoses(path: str | Path) -> dict[str, dict[str, Any]]:
    source = Path(path)
    files = sorted(source.glob("*.json")) if source.is_dir() else [source]
    diagnoses: dict[str, dict[str, Any]] = {}
    for file_path in files:
        item = json.loads(file_path.read_text(encoding="utf-8"))
        if isinstance(item, dict) and isinstance(item.get("run_id"), str):
            diagnoses[item["run_id"]] = item
    return diagnoses


def _run_action(run: dict[str, Any], diagnosis: dict[str, Any]) -> str:
    return str(run.get("action") or run.get("fault_type") or diagnosis.get("action") or "unknown")


def _run_target(run: dict[str, Any], diagnosis: dict[str, Any]) -> str:
    return str(run.get("target") or run.get("target_component") or diagnosis.get("target_component") or "unknown")


def _run_tag(run_id: str) -> str:
    return f"[run: {run_id}]"


def _fault_category(action: str) -> str | None:
    if action in {"restart_container", "stop_container"}:
        return "container interruption"
    if action == "pause_container":
        return "pause"
    if action == "cpu_throttle":
        return "CPU throttle"
    if action == "traffic_burst":
        return "burst"
    return None


def _format_number(value: Any) -> str:
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _representative_claim(finding: dict[str, Any]) -> dict[str, Any] | None:
    claims = finding.get("measured_claims") or []
    if not claims:
        return None
    return max(
        claims,
        key=lambda claim: max(
            (float(value) for value in (claim.get("measurements") or {}).values()
             if isinstance(value, (int, float)) and not isinstance(value, bool)),
            default=-1.0,
        ),
    )


def _finding_lines(findings: list[dict[str, Any]], heading: str, lines: list[str], known_ids: set[str]) -> None:
    lines.extend([f"## {heading}", ""])
    if not findings:
        lines.extend(["No findings in this category.", ""])
        return
    for finding in findings:
        run_id = str(finding.get("run_id") or "unknown")
        if run_id not in known_ids:
            raise ValueError(f"Finding references an unknown run: {run_id}")
        lines.append(f"- **{str(finding.get('severity', 'info')).upper()}** {finding.get('summary', 'Finding without a summary.')} {_run_tag(run_id)}")
        recommendation = finding.get("recommendation")
        if recommendation:
            lines.append(f"  - Recommendation: {recommendation} {_run_tag(run_id)}")
        claim = _representative_claim(finding)
        if claim:
            measures = claim.get("measurements") or {}
            measured = ", ".join(f"{name}={_format_number(value)}" for name, value in sorted(measures.items()))
            window = claim.get("metric_window") or {}
            lines.append(
                f"  - Evidence: {measured}; source `{claim.get('source_file')}`; "
                f"window {window.get('start')} to {window.get('end')} {_run_tag(run_id)}"
            )
    lines.append("")


def _metric_table_row(label: str, value: dict[str, Any], before_id: str, after_id: str) -> str:
    unit = value.get("unit", "")
    def show(number: Any, missing: str) -> str:
        return missing if number is None else f"{_format_number(number)} {unit}".strip()

    before = show(value.get("before"), "not recorded")
    after = show(value.get("after"), "not recorded")
    delta = show(value.get("delta"), "not demonstrated")
    return f"| {label} | {before} {_run_tag(before_id)} | {after} {_run_tag(after_id)} | {delta} |"


def _condition_label(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def build_final_report(
    runs: list[dict[str, Any]],
    diagnoses: dict[str, dict[str, Any]],
    findings_data: dict[str, Any],
    retest: dict[str, Any],
    forced_fixture: dict[str, Any],
    *,
    source_labels: dict[str, str] | None = None,
) -> str:
    """Render supplied analysis artifacts; this function does not create findings or scores."""
    source_labels = source_labels or {}
    real_runs = [run for run in runs if run.get("forced_test") is not True and run.get("dry_run") is not True]
    run_ids = {str(run.get("run_id")) for run in real_runs if run.get("run_id")}
    forced_run_id = str(forced_fixture.get("run_id") or "unknown")
    known_ids = run_ids | {forced_run_id}
    before_id = str(retest.get("before_run_id") or "unknown")
    after_id = str(retest.get("after_run_id") or "unknown")
    known_ids.update({before_id, after_id})

    run_by_id = {str(run.get("run_id")): run for run in real_runs if run.get("run_id")}
    actions: dict[str, set[str]] = {name: set() for name in (
        "container interruption", "pause", "CPU throttle", "burst"
    )}
    lines = [
        "# Findings Report",
        "",
        "## Context",
        "",
        "This deterministic report covers only the available local run artifacts. Live experiment records and saved diagnoses are distinct from reconstructed fixtures; no fault is executed while generating this report.",
        "",
        "## Data Provenance",
        "",
        "- **Live:** primary JSON records in `data/experiments/`, matching saved diagnoses in `data/diagnoses/`, and the ranked findings JSON in `data/findings-ranked.json`.",
        f"- **Fixture:** the Day-1 retest measurements and forced kill-switch record are reconstructed fixtures, not raw live records. {source_labels.get('fixture', 'Their values are labelled at point of use.')} {_run_tag(before_id)} {_run_tag(forced_run_id)}",
        "- Verification sidecars are excluded from the live run count.",
        "",
        "## Summary",
        "",
    ]
    if real_runs:
        lines.append(f"- Live runs reviewed: {', '.join(_run_tag(str(run['run_id'])) for run in real_runs)}")
    else:
        lines.append("- Live runs reviewed: none")
    lines.extend(["", "## Fault-Type Coverage", "", "| Fault type | Status | Live run |", "|---|---|---|"])
    for category, category_actions in (
        ("container interruption", {"restart_container", "stop_container"}),
        ("pause", {"pause_container"}),
        ("CPU throttle", {"cpu_throttle"}),
        ("burst", {"traffic_burst"}),
    ):
        refs = []
        for run in real_runs:
            diagnosis = diagnoses.get(str(run.get("run_id")), {})
            action = _run_action(run, diagnosis)
            if action in category_actions:
                refs.append(_run_tag(str(run["run_id"])))
                actions[category].add(action)
        lines.append(f"| {category} | {'recorded' if refs else 'NOT RUN'} | {', '.join(refs) if refs else 'none'} |")

    lines.extend(["", "## Predicted vs Observed", ""])
    for run_id, run in run_by_id.items():
        diagnosis = diagnoses.get(run_id)
        if not diagnosis:
            lines.append(f"- Diagnosis unavailable for `{run_id}`. {_run_tag(run_id)}")
            continue
        action = _run_action(run, diagnosis)
        target = _run_target(run, diagnosis)
        lines.append(f"### {action} on {target} {_run_tag(run_id)}")
        for sli in ("write", "search", "freshness"):
            comparison = (diagnosis.get("comparison") or {}).get(sli)
            if comparison:
                lines.append(
                    f"- {sli}: predicted {comparison.get('predicted', 'unknown')}; "
                    f"observed {comparison.get('observed', 'inconclusive')}; "
                    f"{comparison.get('verdict', 'inconclusive')} {_run_tag(run_id)}"
                )
        recovery = diagnosis.get("recovery_seconds")
        recovery_text = "unknown" if recovery is None else f"{_format_number(recovery)} s"
        lines.append(f"- Recovery: {diagnosis.get('recovery_status', 'unknown')}, {recovery_text}. {_run_tag(run_id)}")
        baseline = run.get("baseline_id")
        if baseline:
            lines.append(f"- Baseline ID: `{baseline}`. {_run_tag(run_id)}")
        else:
            lines.append(f"- Baseline ID: Not recorded in the run JSON; diagnosis has no baseline values. {_run_tag(run_id)}")
        lines.append("")

    ranked = findings_data.get("ranked_findings") or []
    informational = findings_data.get("informational_results") or []
    _finding_lines(ranked, "Ranked Findings", lines, known_ids)
    _finding_lines(informational, "Informational Results", lines, known_ids)

    lines.extend(["## Resilience Scores", "", "Scores below are copied from the run records; they are not recalculated.", ""])
    lines.extend([
        "| Run | Recorded score | Availability | Containment | Detection | Latency | Recovery |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for run_id, run in run_by_id.items():
        score = run.get("resilience_score") if isinstance(run.get("resilience_score"), dict) else {}
        components = run.get("score_components") if isinstance(run.get("score_components"), dict) else {}
        required_components = {"availability", "containment", "detection", "latency", "recovery"}
        if score.get("score") is None or not required_components.issubset(components):
            reason = "score fields are absent from the run record"
            lines.append(f"| {_run_tag(run_id)} | Not scored: {reason} | - | - | - | - | - |")
            continue
        scalar = _format_number(score["score"])
        cells = [_format_number(components.get(key, "missing")) for key in (
            "availability", "containment", "detection", "latency", "recovery"
        )]
        lines.append(f"| {_run_tag(run_id)} | {scalar} | " + " | ".join(cells) + " |")
    lines.append("")

    forced_target = (forced_fixture.get("_reported_results") or {}).get("target", "unknown")
    forced_action = (forced_fixture.get("_reported_results") or {}).get("action", "unknown")
    lines.extend([
        "## Forced Kill-Switch Test",
        "",
        f"- **Fixture record, not live data:** {forced_action} on {forced_target}; score: **Not scored by design**. Excluded from live findings and resilience scores. {_run_tag(forced_run_id)}",
        "",
        "## Remediation Retest",
        "",
        f"- Before-run measurements are a labelled fixture reconstruction; after-run measurements use the live run record where available. Supplemental request-level measurements are fixture/documented values. {_run_tag(before_id)} {_run_tag(after_id)}",
        "- The live after record omits its baseline ID, although the supplemental summary names one; the discrepancy is retained below. " + _run_tag(after_id),
        "",
        "| Measure | Before | After | Delta |",
        "|---|---|---|---|",
    ])
    measurements = retest.get("measurements") or {}
    for key, label in (
        ("search_outage_seconds", "Total search outage"),
        ("last_search_failure_after_rollback_seconds", "Last failed search after rollback"),
        ("search_failure_wait_seconds", "Wait for one failed search"),
        ("search_latency_p95_ms", "Search latency p95"),
        ("write_error_rate", "Write error rate"),
        ("write_p95_ms", "Write p95"),
        ("recovery_time_seconds", "Recovery after rollback"),
    ):
        value = measurements.get(key) or {"before": None, "after": None, "delta": None, "unit": ""}
        lines.append(_metric_table_row(label, value, before_id, after_id))
    timeouts = measurements.get("search_request_timeouts") or {}
    before_timeout = timeouts.get("before") or {}
    after_timeout = timeouts.get("after") or {}
    timeout_delta = timeouts.get("delta_count")
    timeout_delta_display = (
        str(int(timeout_delta)) if isinstance(timeout_delta, float) and timeout_delta.is_integer()
        else _format_number(timeout_delta) if timeout_delta is not None else None
    )
    timeout_delta_text = "not demonstrated" if timeout_delta_display is None else f"+{timeout_delta_display} requests; windows differ"
    before_timeout_text = "not recorded" if not before_timeout else f"{before_timeout.get('count')}/{before_timeout.get('failures')} over {before_timeout.get('window_seconds')} s"
    after_timeout_text = "not recorded" if not after_timeout else f"{after_timeout.get('count')}/{after_timeout.get('failures')} over {after_timeout.get('window_seconds')} s"
    lines.append(f"| Search request timeouts | {before_timeout_text} {_run_tag(before_id)} | {after_timeout_text} {_run_tag(after_id)} | {timeout_delta_text} {_run_tag(before_id)} {_run_tag(after_id)} |")

    conditions = retest.get("conditions") or {}
    timeout_change = conditions.get("client_timeout_seconds") or {}
    before_client_timeout = timeout_change.get("before")
    after_client_timeout = timeout_change.get("after")
    timeout_change_text = "not recorded" if before_client_timeout is None or after_client_timeout is None else f"{before_client_timeout} s to {after_client_timeout} s; delta {_format_number(timeout_change.get('delta'))} s"
    baseline_p95 = conditions.get("baseline_write_p95_ms") or {}
    baseline_ids = conditions.get("baseline_ids") or {}
    lines.extend([
        "",
        f"- Conditions: same fault = {_condition_label(conditions.get('same_fault'))}; same target = {_condition_label(conditions.get('same_target'))}; same duration = {_condition_label(conditions.get('same_duration'))}. {_run_tag(before_id)} {_run_tag(after_id)}",
        f"- Client timeout: {timeout_change_text}. {_run_tag(before_id)} {_run_tag(after_id)}",
        f"- Baseline write p95: {baseline_p95.get('before')} ms before, {baseline_p95.get('after')} ms after. Delta {_format_number(baseline_p95.get('delta'))} ms. {_run_tag(before_id)} {_run_tag(after_id)}",
        f"- Baseline IDs in raw run records: before `{baseline_ids.get('before') or 'not recorded'}`, after `{baseline_ids.get('after') or 'not recorded'}`; supplemental after summary: `{baseline_ids.get('after_reported_in_summary') or 'not recorded'}`. {_run_tag(before_id)} {_run_tag(after_id)}",
        f"- Conclusion: per-failed-request wait fell from {measurements.get('search_failure_wait_seconds', {}).get('before')} s to {measurements.get('search_failure_wait_seconds', {}).get('after')} s, but overall resilience improvement is **not demonstrated**. Search outage duration and search latency p95 are not recorded for both runs. {_run_tag(before_id)} {_run_tag(after_id)}",
        "",
        "## Evidence Gaps",
        "",
        "- CPU throttle: no live run is present in `data/experiments/`.",
        "- Baseline IDs: absent from the raw run records for the live experiments; diagnosis warnings note missing baseline values for each SLI.",
        f"- Day-1 retest raw record and raw request-level JSONL are unavailable in the working tree; the before values are fixture/documented reconstructions. {_run_tag(before_id)}",
        f"- Search outage total and search latency p95 after the retest are not recorded. {_run_tag(after_id)}",
        "- Scores: all available live experiment records include a scalar resilience score and score components; the forced test is not scored by design.",
        "",
        "## Source Files",
        "",
    ])
    for run_id, run in run_by_id.items():
        source = source_labels.get(run_id, f"data/experiments/run-{run_id.removeprefix('run-')}.json")
        diagnosis_file = source_labels.get(f"diagnosis:{run_id}", f"data/diagnoses/diagnosis-{run_id}.json")
        lines.append(f"- Live run: `{source}`; saved diagnosis: `{diagnosis_file}`. {_run_tag(run_id)}")
    lines.append(f"- Ranked live findings: `data/findings-ranked.json`. " + " ".join(_run_tag(run_id) for run_id in sorted(run_ids)))
    lines.append(f"- Forced-test fixture: `tests/fixtures/m4-forced-test-docs-reconstruction.json`. {_run_tag(forced_run_id)}")
    lines.append(f"- Retest fixtures: `tests/fixtures/restart-retest-before.json` and `tests/fixtures/restart-retest-after.json`. {_run_tag(before_id)} {_run_tag(after_id)}")

    text = "\n".join(lines) + "\n"
    trace_check_markdown(text, known_run_ids=known_ids)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the findings report from local runs, diagnoses, findings, and retest evidence.")
    parser.add_argument("--runs", default=str(DEFAULT_RUNS_PATH), help="Directory or file of live run records.")
    parser.add_argument("--diagnoses", default=str(DEFAULT_DIAGNOSES_PATH), help="Directory containing saved diagnosis JSON files.")
    parser.add_argument("--findings", default=str(DEFAULT_FINDINGS_PATH), help="Ranked findings JSON generated by ai.findings.")
    parser.add_argument("--retest-before", default=str(DEFAULT_RETEST_BEFORE_PATH), help="Fixture reconstruction for the Day-1 retest run.")
    parser.add_argument("--retest-after", default="run-a09cb1bcbb7c", help="Run ID of the live after retest.")
    parser.add_argument("--retest-evidence", default=str(DEFAULT_RETEST_EVIDENCE_PATH), help="Fixture with documented request-level retest evidence.")
    parser.add_argument("--forced-fixture", default=str(DEFAULT_FORCED_FIXTURE_PATH), help="Forced kill-switch test fixture; never scored.")
    parser.add_argument("--out", default=str(DEFAULT_REPORT_PATH), help="Destination Markdown report path.")
    parser.add_argument("--policy", default=str(ROOT / "configs" / "experiment-policy.yaml"), help="Experiment policy used for report context.")
    args = parser.parse_args()
    runs = _load_runs(args.runs)
    diagnoses = _load_diagnoses(args.diagnoses)
    findings_data = json.loads(Path(args.findings).read_text(encoding="utf-8"))
    before = json.loads(Path(args.retest_before).read_text(encoding="utf-8"))
    retest_evidence = json.loads(Path(args.retest_evidence).read_text(encoding="utf-8"))
    after = next((copy.deepcopy(run) for run in runs if run.get("run_id") == args.retest_after), None)
    if after is None:
        parser.error(f"Retest after run {args.retest_after} was not found in {args.runs}.")
    after["client_timeout_seconds"] = retest_evidence.get("client_timeout_seconds")
    after["_reported_results"] = retest_evidence.get("_reported_results", {})
    retest = compare_retest_runs(before, after)
    forced_fixture = json.loads(Path(args.forced_fixture).read_text(encoding="utf-8"))
    report = build_final_report(
        runs,
        diagnoses,
        findings_data,
        retest,
        forced_fixture,
        source_labels={
            "fixture": "The Day-1 raw record is absent; the forced-test record is reconstructed from docs/m4-results.md.",
        },
    )
    Path(args.out).write_text(report, encoding="utf-8")
    print(f"Saved findings report to {args.out}")
    print(f"Included {len([run for run in runs if not run.get('forced_test') and not run.get('dry_run')])} live records, {len(findings_data.get('ranked_findings', []))} ranked findings, and {len(findings_data.get('informational_results', []))} informational results.")


if __name__ == "__main__":
    main()
