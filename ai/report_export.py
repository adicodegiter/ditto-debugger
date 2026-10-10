"""Findings report (task 6.2): docs/findings-report.md from the committed evidence.

Deterministic: reads run records and raw probe samples, runs no fault and calls no LLM.
Measured facts, hypotheses (why) and proposed fixes are kept apart, and every line with a
number carries the run it comes from (checked by ai.trace_check).
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from ai.findings import build_ranked_findings, trace_check_markdown
from ai.ranking import _target_and_action
from ai.request_evidence import load_samples, request_facts
from ai.retest import compare_retest_runs

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = ROOT / "docs" / "findings-report.md"
# Committed evidence (data/ is git-ignored): run records and the raw probe samples around each fault.
DEFAULT_RUNS_PATH = ROOT / "docs" / "evidence" / "windows-host"
DEFAULT_SAMPLES_PATH = DEFAULT_RUNS_PATH / "samples"
DEFAULT_RETEST_BEFORE = "run-c8b30b07c9f3"
DEFAULT_RETEST_AFTER = "run-a09cb1bcbb7c"
DEFAULT_FORCED_FIXTURE_PATH = ROOT / "tests" / "fixtures" / "m4-forced-test-docs-reconstruction.json"
# Configured search client timeouts (not in the run records): docs/m4-results.md and PR #8's SEARCH_TIMEOUT_S.
CLIENT_TIMEOUT_SECONDS = {DEFAULT_RETEST_BEFORE: 10.0, DEFAULT_RETEST_AFTER: 3.0}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


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
        return [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
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


def _diagnosis(run: dict[str, Any], diagnoses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The diagnosis recorded with the run is the evidence; a separately saved one is only a fallback."""
    embedded = run.get("diagnosis")
    if isinstance(embedded, dict) and embedded.get("comparison"):
        return embedded
    return diagnoses.get(str(run.get("run_id")), {})


def _run_action(run: dict[str, Any], diagnosis: dict[str, Any]) -> str:
    explicit = run.get("action") or run.get("fault_type") or diagnosis.get("action")
    return str(explicit or _target_and_action(run)[1])


def _run_target(run: dict[str, Any], diagnosis: dict[str, Any]) -> str:
    explicit = run.get("target") or run.get("target_component") or diagnosis.get("target_component")
    return str(explicit or _target_and_action(run)[0])


def _run_tag(run_id: str) -> str:
    return f"[run: {run_id}]"


def _tags(run_ids: list[str] | set[str]) -> str:
    return " ".join(_run_tag(run_id) for run_id in sorted(set(run_ids)))


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
    """Round floats to at most 3 decimals so raw ratios such as 0.8888888888888888 stay readable."""
    if isinstance(value, float):
        text = f"{round(value, 3):.3f}".rstrip("0").rstrip(".")
        return "0" if text in {"", "-0"} else text
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


def _finding_lines(findings: list[dict[str, Any]], heading: str, lines: list[str], known_ids: set[str],
                   sources: dict[str, str]) -> None:
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
            source = sources.get(run_id, claim.get("source_file"))
            lines.append(
                f"  - Evidence: {measured}; source `{source}`; "
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


def derive_weaknesses(
    runs: list[dict[str, Any]],
    diagnoses: dict[str, dict[str, Any]],
    facts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Name the weaknesses the measurements show, ranked by severity.

    Deterministic rules over recorded facts only. Each item keeps the measured evidence, the
    hypothesis (why) and the proposed fix apart, and carries the runs it is based on.
    """
    weaknesses: list[dict[str, Any]] = []

    def label(run: dict[str, Any]) -> str:
        diagnosis = _diagnosis(run, diagnoses)
        return f"{_run_action(run, diagnosis)} on {_run_target(run, diagnosis)}"

    # 1. Failed searches only ever end as client timeouts: nothing answers with an error.
    hanging = []
    for run in runs:
        f = facts.get(str(run.get("run_id")), {})
        errors = f.get("search_failure_errors") or []
        if f.get("search_failures") and errors and all("Timeout" in error for error in errors):
            hanging.append((run, f))
    if hanging:
        weaknesses.append({
            "severity": "high",
            "title": "Search does not fail fast while things-search is unavailable",
            "evidence": [
                f"{label(run)}: {f['search_failures']} of {f['search_samples']} searches failed, every one a client "
                f"`{', '.join(f['search_failure_errors'])}` after {_format_number(f['search_failure_wait_seconds'])} s; "
                f"search was out for {_format_number(f['search_outage_seconds'])} s {_run_tag(str(run['run_id']))}"
                for run, f in hanging
            ],
            "why": "The gateway holds a search request until the client gives up instead of answering with an error, "
                   "so every caller waits its full timeout, and a caller without a timeout hangs.",
            "fix": "Bound search at the gateway: a request timeout well below the clients', or a circuit breaker on "
                   "the things-search route that answers 503 at once while things-search is unavailable.",
            "validation": "Re-run the things-search restart with a fresh baseline; pass when failed searches return an "
                          "error status (not a client timeout) in under one second.",
            "run_ids": [str(run["run_id"]) for run, _ in hanging],
        })

    # 2. Recovery much slower than the fastest verified recovery for the same target.
    by_target: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        if run.get("recovery_verified") is True and isinstance(run.get("recovery_time_seconds"), (int, float)):
            by_target.setdefault(_run_target(run, _diagnosis(run, diagnoses)), []).append(run)
    for target, group in sorted(by_target.items()):
        fastest = min(group, key=lambda run: run["recovery_time_seconds"])
        slow = [run for run in group if run["recovery_time_seconds"] > max(3 * fastest["recovery_time_seconds"], 10.0)]
        if not slow:
            continue
        weaknesses.append({
            "severity": "medium",
            "title": f"Recovery after a {target} fault is slow and uneven",
            "evidence": [
                f"{label(run)}: recovery verified {_format_number(float(run['recovery_time_seconds']))} s after rollback, "
                f"against {_format_number(float(fastest['recovery_time_seconds']))} s for the fastest {target} run "
                f"{_run_tag(str(run['run_id']))} {_run_tag(str(fastest['run_id']))}"
                for run in slow
            ],
            "why": f"Not measured: {target} may accept traffic before it can serve it again (readiness, cluster "
                   "rejoin or index warm-up), and the recovery check waits until every SLI since rollback is back "
                   "within its threshold. Two runs on different days also differ in load and timing.",
            "fix": f"Gate traffic to {target} on a readiness check that runs a real query, and record when it "
                   "reports ready, so recovery can be split into restart time and warm-up time.",
            "validation": f"Re-run the pause and restart on {target}; pass when recovery is within twice the fastest "
                          "run's recovery.",
            "run_ids": [str(run["run_id"]) for run in slow] + [str(fastest["run_id"])],
        })

    # 3. Predictions that were wrong in the same direction on every measured SLI.
    for verdict, severity, title, fix in (
        ("worse_than_predicted", "high", "Impact was worse than the planner predicted",
         "Treat it as an unexpected coupling: check timeouts, retries and resource limits on the affected path "
         "before widening fault scope."),
        ("better_than_predicted", "low", "The planner over-predicts impact",
         "Update the planner's effect model for this action; for a traffic burst, raise the rate within the "
         "policy's bounds until an SLI degrades, to find the real limit."),
    ):
        matched = []
        for run in runs:
            comparison = _diagnosis(run, diagnoses).get("comparison") or {}
            measured = {sli: v for sli, v in comparison.items() if v.get("verdict") not in (None, "not_measured")}
            if measured and all(v.get("verdict") == verdict for v in measured.values()):
                matched.append((run, measured))
        if matched:
            weaknesses.append({
                "severity": severity,
                "title": title,
                "evidence": [
                    f"{label(run)}: "
                    + "; ".join(f"{sli} predicted {v.get('predicted')}, observed {v.get('observed')}"
                                for sli, v in sorted(measured.items()))
                    + f" {_run_tag(str(run['run_id']))}"
                    for run, measured in matched
                ],
                "why": "Every measured SLI of the run landed on the same side of its prediction.",
                "fix": fix,
                "validation": "Re-plan and re-run with a fresh baseline; pass when the prediction matches on every "
                              "measured SLI.",
                "run_ids": [str(run["run_id"]) for run, _ in matched],
            })

    weaknesses.sort(key=lambda item: SEVERITY_ORDER.get(item["severity"], 9))
    return weaknesses


def build_final_report(
    runs: list[dict[str, Any]],
    diagnoses: dict[str, dict[str, Any]],
    findings_data: dict[str, Any],
    retest: dict[str, Any],
    forced_fixture: dict[str, Any],
    *,
    source_labels: dict[str, str] | None = None,
    facts: dict[str, dict[str, Any]] | None = None,
    sources: dict[str, str] | None = None,
) -> str:
    """Render supplied analysis artifacts; this function does not create findings or scores."""
    source_labels = source_labels or {}
    facts = facts or {}
    sources = sources or {}
    real_runs = [run for run in runs if run.get("forced_test") is not True and run.get("dry_run") is not True]
    run_ids = {str(run.get("run_id")) for run in real_runs if run.get("run_id")}
    forced_run_id = str(forced_fixture.get("run_id") or "unknown")
    forced_is_fixture = bool(forced_fixture.get("_fixture"))
    known_ids = run_ids | {forced_run_id}
    before_id = str(retest.get("before_run_id") or "unknown")
    after_id = str(retest.get("after_run_id") or "unknown")
    known_ids.update({before_id, after_id})

    run_by_id = {str(run.get("run_id")): run for run in real_runs if run.get("run_id")}
    weaknesses = derive_weaknesses(real_runs, diagnoses, facts)
    scored = {
        run_id: float(run["resilience_score"]["score"])
        for run_id, run in run_by_id.items()
        if isinstance(run.get("resilience_score"), dict) and run["resilience_score"].get("status") == "complete"
        and isinstance(run["resilience_score"].get("score"), (int, float))
    }
    categories = sorted({c for run in real_runs if (c := _fault_category(_run_action(run, _diagnosis(run, diagnoses))))})

    lines = [
        "# Findings Report",
        "",
        "## Context",
        "",
        "Generated by `python -m ai.report_export` from the committed run records in `docs/evidence/windows-host/` "
        "and the raw probe samples cut from each run's fault window. It runs no fault and calls no LLM. Every line "
        "with a number names the run it comes from, and `python -m ai.trace_check docs/findings-report.md` checks that.",
        "",
        "## Summary",
        "",
        f"- Live runs reviewed: {', '.join(_run_tag(run_id) for run_id in sorted(run_ids)) or 'none'}; "
        f"fault types covered: {', '.join(categories) or 'none'}.",
    ]
    if weaknesses:
        top = weaknesses[0]
        lines.append(f"- Most severe weakness ({top['severity']}): {top['title']}. {_tags(top['run_ids'])}")
    if scored:
        mean = sum(scored.values()) / len(scored)
        lines.append(
            f"- Resilience score: **{_format_number(mean)} / 100**, the mean of the {len(scored)} scored runs "
            f"(range {_format_number(min(scored.values()))} to {_format_number(max(scored.values()))}). {_tags(set(scored))}"
        )
    if retest.get("overall_improvement"):
        lines.append(f"- Remediation re-test: {retest['overall_improvement']}. {_run_tag(before_id)} {_run_tag(after_id)}")

    lines.extend(["", "## Fault-Type Coverage", "", "| Fault type | Status | Live run |", "|---|---|---|"])
    for category, category_actions in (
        ("container interruption", {"restart_container", "stop_container"}),
        ("pause", {"pause_container"}),
        ("CPU throttle", {"cpu_throttle"}),
        ("burst", {"traffic_burst"}),
    ):
        refs = [_run_tag(str(run["run_id"])) for run in real_runs
                if _run_action(run, _diagnosis(run, diagnoses)) in category_actions]
        lines.append(f"| {category} | {'recorded' if refs else 'NOT RUN'} | {', '.join(refs) if refs else 'none'} |")

    lines.extend(["", "## Weaknesses (ranked by severity)", ""])
    if not weaknesses:
        lines.extend(["No weakness is shown by the recorded runs.", ""])
    for number, weakness in enumerate(weaknesses, start=1):
        lines.extend([
            f"### {number}. [{weakness['severity'].upper()}] {weakness['title']} {_tags(weakness['run_ids'])}",
            "",
            "**Measured**",
        ])
        lines.extend(f"- {item}" for item in weakness["evidence"])
        lines.extend([
            "",
            f"**Why (hypothesis):** {weakness['why']} {_tags(weakness['run_ids'])}",
            "",
            f"**Recommended fix:** {weakness['fix']} {_tags(weakness['run_ids'])}",
            "",
            f"**Validation test:** {weakness['validation']} {_tags(weakness['run_ids'])}",
            "",
        ])
    burst_runs = [str(run["run_id"]) for run in real_runs if _run_action(run, _diagnosis(run, diagnoses)) == "traffic_burst"]
    if burst_runs:
        lines.extend([
            "### Fixed during testing: control-plane deadline",
            "",
            "Faults planned at exactly the policy's maximum duration always aborted on the kill switch's deadline check, "
            "because its last poll lands just after the planned end. The action defaults now leave headroom under the cap, "
            "and no limit was loosened; the burst run below used the corrected default. Details in `docs/m5-results.md`. "
            + _tags(burst_runs),
            "",
        ])
    lines.extend(["## Predicted vs Observed", ""])
    for run_id, run in run_by_id.items():
        diagnosis = _diagnosis(run, diagnoses)
        lines.append(f"### {_run_action(run, diagnosis)} on {_run_target(run, diagnosis)} {_run_tag(run_id)}")
        if not diagnosis.get("comparison"):
            lines.append(f"- No diagnosis in this record: it pre-dates diagnosis on real runs. {_run_tag(run_id)}")
        for sli in ("write", "search", "freshness"):
            comparison = (diagnosis.get("comparison") or {}).get(sli)
            if comparison:
                lines.append(
                    f"- {sli}: predicted {comparison.get('predicted', 'unknown')}; "
                    f"observed {comparison.get('observed', 'inconclusive')}; "
                    f"{comparison.get('verdict', 'inconclusive')} {_run_tag(run_id)}"
                )
        if run.get("recovery_verified") is True and isinstance(run.get("recovery_time_seconds"), (int, float)):
            lines.append(f"- Recovery: verified {_format_number(float(run['recovery_time_seconds']))} s after rollback. {_run_tag(run_id)}")
        else:
            lines.append(f"- Recovery: not verified in this record. {_run_tag(run_id)}")
        lines.append("")

    _finding_lines(findings_data.get("ranked_findings") or [], "Per-Run Findings", lines, known_ids, sources)
    _finding_lines(findings_data.get("informational_results") or [], "Informational Results", lines, known_ids, sources)

    lines.extend([
        "## Resilience Scores",
        "",
        "Copied from the run records, not recalculated; weights and formulas are in `safety/scoring.py` and "
        "`docs/metrics-and-scoring.md`.",
        "",
        "| Run | Fault | Score | Availability | Latency | Recovery | Containment | Detection |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    for run_id, run in run_by_id.items():
        score = run.get("resilience_score") if isinstance(run.get("resilience_score"), dict) else {}
        components = run.get("score_components") if isinstance(run.get("score_components"), dict) else {}
        fault = _run_action(run, _diagnosis(run, diagnoses))
        if score.get("score") is None:
            lines.append(f"| {_run_tag(run_id)} | {fault} | not scored: the record pre-dates scoring | - | - | - | - | - |")
            continue
        cells = [_format_number(float(components[key])) if isinstance(components.get(key), (int, float)) else "missing"
                 for key in ("availability", "latency", "recovery", "containment", "detection")]
        lines.append(f"| {_run_tag(run_id)} | {fault} | {_format_number(float(score['score']))} | " + " | ".join(cells) + " |")
    lines.append("")

    reported = forced_fixture.get("_reported_results") or {}
    forced_target, forced_action = _target_and_action(forced_fixture)
    forced_target = reported.get("target", forced_target)
    forced_action = reported.get("action", forced_action)
    forced_label = "Fixture reconstruction, not a live record" if forced_is_fixture else "Live record"
    forced_detail = ""
    if not forced_is_fixture and forced_fixture.get("abort_reason"):
        forced_detail = (f" The kill switch aborted it (`{forced_fixture['abort_reason']}`), forced_test_passed is "
                         f"{str(forced_fixture.get('forced_test_passed')).lower()}, and recovery was verified "
                         f"{_format_number(float(forced_fixture.get('recovery_time_seconds') or 0.0))} s after rollback.")
    client_timeout = (retest.get("conditions") or {}).get("client_timeout_seconds") or {}
    if client_timeout.get("before") is not None and client_timeout.get("after") is not None:
        timeout_change = (f"the search client timeout went from {_format_number(float(client_timeout['before']))} s to "
                          f"{_format_number(float(client_timeout['after']))} s (client side, PR #8); Ditto itself was not changed")
    else:
        timeout_change = "client timeout change not recorded"
    lines.extend([
        "## Forced Kill-Switch Test",
        "",
        f"- **{forced_label}:** {forced_action} on {forced_target}, with the write-latency limit lowered on purpose.{forced_detail} "
        f"Not scored by design, and excluded from the findings and scores above. {_run_tag(forced_run_id)}",
        "",
        "## Remediation Retest",
        "",
        f"- Remediation under test: {timeout_change}. {_run_tag(before_id)} {_run_tag(after_id)}",
        f"- Request-level numbers come from the raw probe samples of each run's window, from 5 s before injection "
        f"to 5 s after recovery. {_run_tag(before_id)} {_run_tag(after_id)}",
        "",
        "| Measure | Before | After | Delta |",
        "|---|---|---|---|",
    ])
    measurements = retest.get("measurements") or {}
    for key, row_label in (
        ("search_failure_wait_seconds", "Wait for one failed search"),
        ("search_outage_seconds", "Total search outage"),
        ("last_search_failure_after_rollback_seconds", "Last failed search after rollback"),
        ("write_samples_per_second", "Write probe rate in the window"),
        ("write_error_rate", "Write error rate"),
        ("write_p95_ms", "Write p95 during the fault"),
        ("search_latency_p95_ms", "Search latency p95"),
        ("recovery_time_seconds", "Recovery after rollback"),
    ):
        value = measurements.get(key) or {"before": None, "after": None, "delta": None, "unit": ""}
        lines.append(_metric_table_row(row_label, value, before_id, after_id))
    timeouts = measurements.get("search_request_timeouts") or {}

    def timeout_text(item: dict[str, Any]) -> str:
        if not item:
            return "not recorded"
        return f"{item.get('failures')} in {_format_number(float(item.get('window_seconds') or 0))} s"

    lines.append(
        f"| Failed searches (all client timeouts) | {timeout_text(timeouts.get('before') or {})} {_run_tag(before_id)} | "
        f"{timeout_text(timeouts.get('after') or {})} {_run_tag(after_id)} | windows differ in length {_run_tag(before_id)} {_run_tag(after_id)} |"
    )

    conditions = retest.get("conditions") or {}
    baseline_p95 = conditions.get("baseline_write_p95_ms") or {}
    improvement = retest.get("overall_improvement", "not demonstrated")
    conclusion_detail = ""
    if improvement.startswith("client-side improvement demonstrated"):
        conclusion_detail = " A failed search costs less time, and the other probes kept their rate during the outage."
        if "server-side fail-fast not demonstrated" in improvement:
            conclusion_detail += (" Every failure is still a client timeout, so Ditto itself does not answer fast"
                                  + (f" (weakness: {weaknesses[0]['title'].lower()})" if weaknesses and weaknesses[0]["title"].startswith("Search does not fail fast") else "")
                                  + ".")
    recovery = measurements.get("recovery_time_seconds") or {}
    if isinstance(recovery.get("delta"), (int, float)) and recovery["delta"] > 0:
        conclusion_detail += (" Recovery was slower in the re-run; with one run on each side this is reported as variance"
                              " to watch, not as an effect of the fix.")
    lines.extend([
        "",
        f"- Conditions: same fault = {_condition_label(conditions.get('same_fault'))}; same target = "
        f"{_condition_label(conditions.get('same_target'))}; same duration = {_condition_label(conditions.get('same_duration'))}. "
        f"Baseline write p95 was {baseline_p95.get('before')} ms before and {baseline_p95.get('after')} ms after. "
        f"{_run_tag(before_id)} {_run_tag(after_id)}",
        f"- Conclusion: **{improvement}**.{conclusion_detail} {_run_tag(before_id)} {_run_tag(after_id)}",
        "",
        "## Evidence Gaps",
        "",
        "- CPU throttle: implemented and allowlisted, but no live run is recorded.",
        "- Run records do not store their baseline ID; the IDs used are listed in `docs/m5-results.md`. " + _tags(run_ids),
        *([f"- The re-test's before record pre-dates diagnosis and scoring, so it has neither. {_run_tag(before_id)}"]
          if before_id in run_by_id and not _diagnosis(run_by_id[before_id], diagnoses).get("comparison") else []),
        "- Search latency p95 is not recorded per window; only search error rates are. " + _tags(run_ids),
        "- The forced kill-switch test is not scored by design.",
        "",
        "## Source Files",
        "",
    ])
    for run_id in sorted(run_by_id):
        source = sources.get(run_id, source_labels.get(run_id, f"data/experiments/{run_id}.json"))
        sample_note = f"; samples `{sources[f'samples:{run_id}']}`" if f"samples:{run_id}" in sources else ""
        lines.append(f"- `{source}`{sample_note} {_run_tag(run_id)}")
    forced_source = sources.get(forced_run_id) or ("tests/fixtures/m4-forced-test-docs-reconstruction.json" if forced_is_fixture else "")
    if forced_source:
        lines.append(f"- Forced test: `{forced_source}` {_run_tag(forced_run_id)}")

    text = "\n".join(lines) + "\n"
    trace_check_markdown(text, known_run_ids=known_ids)
    return text


def _with_request_facts(run: dict[str, Any], run_facts: dict[str, Any]) -> dict[str, Any]:
    """The run plus its request-level facts, in the fields ai.retest reads."""
    enriched = copy.deepcopy(run)
    reported = dict(enriched.get("_reported_results") or {})
    reported.update({key: value for key, value in run_facts.items() if value is not None})
    enriched["_reported_results"] = reported
    if run.get("run_id") in CLIENT_TIMEOUT_SECONDS:
        enriched["client_timeout_seconds"] = CLIENT_TIMEOUT_SECONDS[run["run_id"]]
    diagnosis = run.get("diagnosis") or {}
    enriched.setdefault("action", _run_action(run, diagnosis))
    enriched.setdefault("target", _run_target(run, diagnosis))
    return enriched


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate docs/findings-report.md from the committed run records and probe samples.")
    parser.add_argument("--runs", default=str(DEFAULT_RUNS_PATH), help="Directory or file of run records (default: docs/evidence/windows-host).")
    parser.add_argument("--samples", default=str(DEFAULT_SAMPLES_PATH), help="Directory of <run_id>.jsonl probe samples (python -m ai.request_evidence).")
    parser.add_argument("--diagnoses", help="Optional saved diagnoses, used only for records without an embedded diagnosis.")
    parser.add_argument("--retest-before", default=DEFAULT_RETEST_BEFORE, help="Run ID before the remediation.")
    parser.add_argument("--retest-after", default=DEFAULT_RETEST_AFTER, help="Run ID after the remediation.")
    parser.add_argument("--forced-fixture", default=str(DEFAULT_FORCED_FIXTURE_PATH), help="Used only when no forced-test record is among --runs.")
    parser.add_argument("--out", default=str(DEFAULT_REPORT_PATH), help="Destination Markdown report path.")
    args = parser.parse_args()

    runs_path = Path(args.runs)
    files = sorted(runs_path.glob("run-*.json")) if runs_path.is_dir() else [runs_path]
    records = [(json.loads(path.read_text(encoding="utf-8")), path) for path in files]
    runs = [record for record, _ in records]

    def rel(path: Path) -> str:
        path = path.resolve()
        return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.as_posix()

    sources = {str(record.get("run_id")): rel(path) for record, path in records}
    diagnoses = _load_diagnoses(args.diagnoses) if args.diagnoses else {}
    facts: dict[str, dict[str, Any]] = {}
    for run in runs:
        sample_file = Path(args.samples) / f"{run.get('run_id')}.jsonl"
        if sample_file.exists():
            facts[str(run["run_id"])] = request_facts(run, load_samples(sample_file))
            sources[f"samples:{run['run_id']}"] = rel(sample_file)

    by_id = {str(run.get("run_id")): run for run in runs}
    for run_id in (args.retest_before, args.retest_after):
        if run_id not in by_id:
            parser.error(f"Re-test run {run_id} was not found in {args.runs}.")
    retest = compare_retest_runs(
        _with_request_facts(by_id[args.retest_before], facts.get(args.retest_before, {})),
        _with_request_facts(by_id[args.retest_after], facts.get(args.retest_after, {})),
    )
    findings_data = build_ranked_findings(records)
    forced = next((run for run in runs if run.get("forced_test") is True), None)
    if forced is None:
        forced = json.loads(Path(args.forced_fixture).read_text(encoding="utf-8"))
    report = build_final_report(runs, diagnoses, findings_data, retest, forced, facts=facts, sources=sources)
    Path(args.out).write_text(report, encoding="utf-8", newline="\n")
    print(f"Saved findings report to {args.out}")
    print(f"Included {len([r for r in runs if not r.get('forced_test') and not r.get('dry_run')])} live records, "
          f"{len(findings_data.get('ranked_findings', []))} ranked findings, "
          f"{len(findings_data.get('informational_results', []))} informational results.")


if __name__ == "__main__":
    main()
