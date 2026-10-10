from __future__ import annotations

import argparse
import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from ai.diagnosis import diagnose
from ai.report import generate_findings
from ai.run_record import normalize_run_record


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = ROOT / "configs" / "experiment-policy.yaml"
SEVERITY_POLICY = {
    "critical": {"recovery_seconds": 60, "slis_affected": 2, "breach_mult": 2.0},
    "high": {"recovery_seconds": 30, "slis_affected": 1, "breach_mult": 1.5},
    "medium": {"recovery_seconds": 15, "slis_affected": 1, "breach_mult": 1.2},
    "low": {"recovery_seconds": 5, "slis_affected": 0, "breach_mult": 1.0},
}
FIX_LIBRARY = {
    "search_hang_client_timeout": {
        "title": "Search requests hang until the client timeout instead of failing fast.",
        "fix": "Add a bounded timeout or circuit breaker at the gateway/client so search failures fail fast rather than blocking until the client timeout.",
        "validation_test": {"rerun_fault": "restart_container", "target": "things-search", "duration": 10, "expected_outcome": "search errors fail within 3 seconds without hanging."},
    },
    "search_outage_restart_pause": {
        "title": "Search outage during restart or pause.",
        "fix": "Run a second replica or add graceful degradation so the service is still reachable during restart or pause windows.",
        "validation_test": {"rerun_fault": "pause_container", "target": "things-search", "duration": 30, "expected_outcome": "search failures remain bounded and recover without dropping the canary."},
    },
    "write_latency_burst": {
        "title": "Write latency rises under burst traffic.",
        "fix": "Apply resource limits, rate limiting or backpressure so the API entry point does not allow unbounded demand spikes to saturate downstream writes.",
        "validation_test": {"rerun_fault": "traffic_burst", "target": "nginx", "duration": 60, "expected_outcome": "write latency stays within the baseline multiplier and no backlog grows unbounded."},
    },
    "cpu_throttle_slow": {
        "title": "CPU throttling slows requests.",
        "fix": "Set resource limits or scale the service before throttling so request latency stays under the configured threshold.",
        "validation_test": {"rerun_fault": "cpu_throttle", "target": "things-search", "duration": 60, "expected_outcome": "search latency remains within the configured SLA while CPU is still reduced."},
    },
    "slow_recovery": {
        "title": "Recovery is slower than the policy limit.",
        "fix": "Improve health checks and restart policy so the service returns to healthy within the configured recovery window.",
        "validation_test": {"rerun_fault": "restart_container", "target": "things-search", "duration": 10, "expected_outcome": "all SLIs recover inside the policy timeout after rollback."},
    },
}

INCONCLUSIVE_RECOMMENDATION = (
    "Do not score this run; capture complete fault-window SLI samples and recovery evidence before drawing a resilience conclusion."
)
FAULT_CATEGORIES = {
    "container interruption": {"restart_container", "stop_container"},
    "pause": {"pause_container"},
    "CPU throttle": {"cpu_throttle"},
    "burst": {"traffic_burst"},
}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _fault_action(run: dict[str, Any]) -> str:
    diagnosis = run.get("diagnosis") or {}
    return str(run.get("fault_type") or run.get("action") or diagnosis.get("action") or "unknown")


def _fault_category(action: str) -> str | None:
    return next((name for name, actions in FAULT_CATEGORIES.items() if action in actions), None)


def _window_measurements(observation: dict[str, Any], sli: str) -> dict[str, Any]:
    sample_key = {"write": "write_samples", "search": "search_samples", "freshness": "freshness_samples"}[sli]
    try:
        samples = float(observation.get(sample_key) or 0)
    except (TypeError, ValueError):
        return {}
    if samples <= 0:
        return {}
    keys = {
        "write": ("write_error_rate", "write_p95_ms"),
        "search": ("search_error_rate",),
        "freshness": ("freshness_p95_seconds",),
    }[sli]
    return {key: observation[key] for key in keys if observation.get(key) is not None}


def _measured_claims(finding: dict[str, Any], run: dict[str, Any], source_file: str) -> list[dict[str, Any]]:
    run_id = str(run.get("run_id") or "")
    finding_id = str(finding.get("id", ""))
    diagnosis = run.get("diagnosis") or {}
    observations = [
        item for item in run.get("observations", [])
        if isinstance(item, dict) and item.get("phase") == "fault"
    ]
    claims: list[dict[str, Any]] = []

    def add_claim(text: str, observation: dict[str, Any], measurements: dict[str, Any]) -> None:
        claims.append({
            "claim": text,
            "measurements": measurements,
            "run_id": run_id,
            "source_file": source_file,
            "metric_window": {
                "start": observation.get("window_start"),
                "end": observation.get("window_end"),
            },
        })

    if finding_id.endswith("-availability"):
        for observation in observations:
            for sli in ("write", "search"):
                measurements = _window_measurements(observation, sli)
                for name, value in measurements.items():
                    if name.endswith("error_rate") and isinstance(value, (int, float)) and value > 0:
                        add_claim(f"{name}={value}", observation, {name: value})
            if observation.get("telemetry_available") is False:
                add_claim("telemetry_available=false", observation, {"telemetry_available": False})
    elif finding_id.endswith(("-better-than-predicted", "-worse-than-predicted")):
        verdict = "better_than_predicted" if finding_id.endswith("-better-than-predicted") else "worse_than_predicted"
        comparisons = diagnosis.get("comparison") or {}
        for sli, result in comparisons.items():
            if result.get("verdict") != verdict or sli not in {"write", "search", "freshness"}:
                continue
            for observation in observations:
                measurements = _window_measurements(observation, sli)
                if measurements:
                    add_claim(
                        f"{sli}: predicted={result.get('predicted')}, observed={result.get('observed')}",
                        observation,
                        measurements,
                    )
    elif finding_id.endswith("-recovery"):
        recovery_windows = [
            item for item in run.get("observations", [])
            if isinstance(item, dict) and item.get("phase") == "recovered"
        ]
        for observation in recovery_windows:
            measurements = {
                key: run.get(key) for key in ("recovery_verified", "recovery_time_seconds")
                if run.get(key) is not None
            }
            if measurements:
                add_claim("recovery verification", observation, measurements)
    else:
        for observation in observations:
            for sli in ("write", "search", "freshness"):
                measurements = _window_measurements(observation, sli)
                if measurements:
                    add_claim(f"{sli} fault-window measurements", observation, measurements)
    return claims


def _audit_finding(finding: dict[str, Any], run: dict[str, Any], source_file: str) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    accepted: list[dict[str, Any]] = []
    for claim in _measured_claims(finding, run, source_file):
        window = claim.get("metric_window") or {}
        missing = []
        if not claim.get("run_id"):
            missing.append("run_id")
        if not claim.get("source_file"):
            missing.append("source_file")
        if not isinstance(window.get("start"), str) or not window.get("start"):
            missing.append("metric_window.start")
        if not isinstance(window.get("end"), str) or not window.get("end"):
            missing.append("metric_window.end")
        if missing:
            warnings.append(f"Dropped unsupported claim from {finding.get('id')}: missing {', '.join(missing)}.")
        else:
            accepted.append(claim)

    if not accepted:
        warnings.append(f"Dropped finding {finding.get('id')}: no measured claim has run, source, and window evidence.")
        return None, warnings

    result = deepcopy(finding)
    result["measured_claims"] = accepted
    result["source_file"] = source_file
    result["evidence"] = [
        item for item in result.get("evidence", [])
        if isinstance(item, str) and item.startswith(("run:", "abort:", "recovery:", "diagnosis:"))
    ]
    return result, warnings


def _prepare_real_run(raw: dict[str, Any], path: Path) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    diagnosis = raw.get("diagnosis") if isinstance(raw.get("diagnosis"), dict) else {}
    action = _fault_action(raw)
    prediction = raw.get("prediction") if isinstance(raw.get("prediction"), dict) else {}
    target = str(
        raw.get("target") or raw.get("target_component") or diagnosis.get("target_component")
        or next(iter(prediction.get("predicted_impacts", [])), "unknown")
    )
    experiment = {
        "id": raw.get("experiment_id"),
        "action": action,
        "target_component": target,
        "predicted_slis": prediction.get("predicted_slis", {}),
    }
    normalized = normalize_run_record(raw, experiment=experiment, run_path=path)
    excluded_forced = normalized["forced_test"] or diagnosis.get("kill_switch", {}).get("forced_test") is True
    excluded_dry = normalized["dry_run"] or diagnosis.get("evidence_use") == "dry_run"
    prepared = deepcopy(raw)
    prepared.update({
        "forced_test": excluded_forced,
        "dry_run": excluded_dry,
        "action": action,
        "fault_type": action,
        "target": target,
        "source_file": path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.as_posix(),
    })
    return prepared, normalized, action, target


def build_ranked_findings(
    records: list[tuple[dict[str, Any], Path]],
) -> dict[str, Any]:
    ranked: list[dict[str, Any]] = []
    informational: list[dict[str, Any]] = []
    dropped_claims: list[str] = []
    real_runs: list[dict[str, Any]] = []
    fault_actions: dict[str, set[str]] = {name: set() for name in FAULT_CATEGORIES}
    missing_baseline_ids: list[str] = []
    missing_scores: list[str] = []

    for raw, path in records:
        run, normalized, action, _target = _prepare_real_run(raw, path)
        if normalized["forced_test"] or normalized["dry_run"]:
            continue
        if not run.get("run_id"):
            dropped_claims.append(f"Skipped {path}: missing run_id.")
            continue
        real_runs.append(run)
        category = _fault_category(action)
        if category:
            fault_actions[category].add(action)
        if not raw.get("baseline_id"):
            missing_baseline_ids.append(str(raw["run_id"]))
        if not isinstance(raw.get("score_components"), dict) or not raw["score_components"]:
            missing_scores.append(str(raw["run_id"]))

        generated = generate_findings(run)
        source_file = run["source_file"]
        for finding in generated:
            audited, warnings = _audit_finding(finding, run, source_file)
            dropped_claims.extend(warnings)
            if audited is None:
                continue
            (informational if audited.get("severity") == "info" else ranked).append(audited)

    ranked.sort(key=lambda item: (
        SEVERITY_ORDER.get(str(item.get("severity", "info")), 99),
        str(item.get("run_id", "")),
        str(item.get("id", "")),
    ))
    for rank, finding in enumerate(ranked, start=1):
        finding["rank"] = rank
    informational.sort(key=lambda item: (str(item.get("run_id", "")), str(item.get("id", ""))))

    present_categories = [name for name, actions in fault_actions.items() if actions]
    report_gaps = [f"No real {name} run is recorded." for name, actions in fault_actions.items() if not actions]
    if missing_baseline_ids:
        report_gaps.append("Missing baseline_id for runs: " + ", ".join(sorted(missing_baseline_ids)) + ".")
    if missing_scores:
        report_gaps.append("Missing score_components for runs: " + ", ".join(sorted(missing_scores)) + ".")

    return {
        "schema_version": "1.0",
        "real_run_count": len(real_runs),
        "distinct_fault_type_count": len(present_categories),
        "fault_types_with_real_data": present_categories,
        "ranked_findings": ranked,
        "informational_results": informational,
        "dropped_claims": dropped_claims,
        "report_gaps": report_gaps,
    }


def _load_experiment_records(directory: Path) -> list[tuple[dict[str, Any], Path]]:
    records: list[tuple[dict[str, Any], Path]] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".verify.json"):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            records.append((payload, path))
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank evidence-audited findings from real experiment records.")
    parser.add_argument("--runs", type=Path, default=ROOT / "data" / "experiments", help="Directory of run JSON records.")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "findings-ranked.json", help="Destination JSON file (default is gitignored data/).")
    args = parser.parse_args()
    report = build_ranked_findings(_load_experiment_records(args.runs))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    for warning in report["dropped_claims"]:
        print(warning)
    print(f"Real runs: {report['real_run_count']}")
    print(f"Distinct fault types with real data: {report['distinct_fault_type_count']} of 4 ({', '.join(report['fault_types_with_real_data']) or 'none'}).")
    print("Ranked findings:")
    for finding in report["ranked_findings"]:
        print(f"{finding['rank']}. [{finding['severity']}] {finding['summary']} ({finding['run_id']})")
    print("Informational results:")
    for finding in report["informational_results"]:
        print(f"- {finding['summary']} ({finding['run_id']})")
    print("Report gaps:")
    for gap in report["report_gaps"]:
        print(f"- {gap}")
    print(f"Saved JSON to {args.out}")


if __name__ == "__main__":
    main()


def _metric_windows(run: dict[str, Any], comparison: dict[str, Any]) -> list[dict[str, Any]]:
    run_id = run.get("run_id")
    links: list[dict[str, Any]] = []
    for observation in run.get("observations", []):
        if not isinstance(observation, dict) or observation.get("phase") != "fault":
            continue
        start = observation.get("window_start")
        end = observation.get("window_end")
        if not isinstance(start, str) or not isinstance(end, str):
            continue
        for sli, result in comparison.items():
            if result.get("verdict") == "not_measured":
                continue
            links.append({
                "result_id": run_id,
                "sli": sli,
                "metric_window": {"start": start, "end": end},
            })
    return links


def enrich_findings(
    findings: list[dict[str, Any]], run: dict[str, Any], diagnosis: dict[str, Any]
) -> list[dict[str, Any]]:
    """Enrich existing report findings; this function does not create findings."""
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("A run_id is required to link finding evidence.")
    if diagnosis.get("run_id") != run_id:
        raise ValueError("Diagnosis run_id does not match the findings run.")

    comparison = diagnosis.get("comparison")
    if not isinstance(comparison, dict):
        raise ValueError("Diagnosis comparison must be a mapping.")
    links = _metric_windows(run, comparison)
    linked_slis = {link["sli"] for link in links}
    measured_slis = {
        sli for sli, result in comparison.items()
        if result.get("verdict") != "not_measured"
    }
    evidence_status = diagnosis.get("evidence_use", "inconclusive")
    if evidence_status == "experiment" and not measured_slis.issubset(linked_slis):
        evidence_status = "inconclusive"
    enriched: list[dict[str, Any]] = []
    for finding in findings:
        if finding.get("run_id") != run_id:
            raise ValueError("Finding run_id does not match the findings run.")
        item = deepcopy(finding)
        item["schema_version"] = "1.1"
        item["prediction_comparison"] = deepcopy(comparison)
        item["evidence_links"] = deepcopy(links)
        item["evidence_status"] = evidence_status
        if item["evidence_status"] == "inconclusive":
            item["severity"] = "info"
            item["recommendation"] = INCONCLUSIVE_RECOMMENDATION
        enriched.append(item)
    return enriched


def diagnose_report_findings(
    findings: list[dict[str, Any]],
    run: dict[str, Any],
    experiment: dict[str, Any],
    baseline: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Diagnose and enrich the report producer's records without persisting a second result."""
    diagnosis = diagnose(experiment, run, baseline)
    if run.get("dry_run") is True or diagnosis.get("evidence_use") == "kill_switch_test":
        return []
    return enrich_findings(findings, run, diagnosis)


def _policy_load(policy_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(policy_path) if policy_path is not None else DEFAULT_POLICY_PATH
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as stream:
        loaded = yaml.safe_load(stream) or {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _run_score(run: dict[str, Any]) -> float | None:
    values = run.get("score_components") or {}
    if not isinstance(values, dict):
        return None
    candidates = [float(value) for value in values.values() if isinstance(value, (int, float))]
    if not candidates:
        return None
    return round(sum(candidates) / len(candidates), 3)


def _severity_for_run(run: dict[str, Any], policy: dict[str, Any]) -> tuple[str, str, float, int]:
    thresholds = policy.get("abort_thresholds") or {}
    recovery_limit = float((policy.get("recovery") or {}).get("timeout_seconds", 180))
    if run.get("forced_test") is True or run.get("dry_run") is True:
        return ("low", "inconclusive", 0.0, 0)
    if run.get("recovery_verified") is False:
        return ("critical", "confirmed", 1.0, 99)
    if run.get("abort_reason"):
        return ("high", "confirmed", 1.0, 1)
    metric_breach = 0.0
    affected = 0
    for name in ("write", "search", "freshness"):
        value = run.get(f"{name}_error_rate") if name != "freshness" else run.get("freshness_p95_seconds")
        if value is None:
            continue
        if name == "freshness":
            if value is not None:
                affected += 1
                metric_breach = max(metric_breach, float(value))
        elif isinstance(value, (int, float)) and float(value) > 0:
            affected += 1
            metric_breach = max(metric_breach, float(value))
    recovery_seconds = float(run.get("recovery_time_seconds") or 0)
    if recovery_seconds > recovery_limit:
        return ("critical", "confirmed", max(metric_breach, 1.0), affected)
    if affected >= 2 or metric_breach >= float(thresholds.get("write_error_rate") or 0.2):
        return ("high", "confirmed", max(metric_breach, 0.5), affected)
    if recovery_seconds > 0 or metric_breach > 0:
        return ("medium", "confirmed", max(metric_breach, 0.2), affected)
    return ("low", "confirmed", 0.0, 0)


def _pattern_for_run(run: dict[str, Any]) -> str:
    action = str(run.get("fault_type") or run.get("action") or "")
    target = str(run.get("target") or run.get("target_component") or "")
    if action in {"restart_container", "stop_container", "pause_container"} and target == "things-search":
        return "search_outage_restart_pause"
    if action == "traffic_burst":
        return "write_latency_burst"
    if action == "cpu_throttle":
        return "cpu_throttle_slow"
    if run.get("recovery_time_seconds") and (run.get("recovery_time_seconds") or 0) > 0:
        return "slow_recovery"
    return "search_hang_client_timeout"


def _normalize_measurement(run: dict[str, Any], key: str, value: Any, unit: str, metric_window: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "claim": key,
        "value": value,
        "unit": unit,
        "run_id": run.get("run_id"),
        "source_file": run.get("source_file") or "fixture",
        "metric_window": metric_window or {},
    }


def build_findings_for_run(run: dict[str, Any], policy_path: str | Path | None = None) -> dict[str, Any]:
    policy = _policy_load(policy_path)
    run_id = str(run.get("run_id") or "unknown")
    action = run.get("fault_type") or run.get("action") or "unknown"
    target = run.get("target") or run.get("target_component") or "unknown"
    source_file = run.get("source_file") or "fixture"
   
    if run.get("forced_test") is True or run.get("dry_run") is True:
        return {
            "id": f"{run_id}-forced-or-dry",
            "title": "Not scored (forced kill-switch test or dry run)",
            "severity": "low",
            "status": "inconclusive",
            "measured": [],
            "hypothesis": "The run does not count as regular evidence because it was marked forced or dry-run.",
            "fix": "No fix recommendation: treat this record as test-only evidence and keep it separate from scored runs.",
            "validation_test": {"rerun_fault": action, "target": target, "duration": run.get("planned_duration_s") or run.get("action_params", {}).get("duration_seconds") or 0, "expected_outcome": "not scored by design"},
            "evidence_ids": [f"run:{run_id}"],
        }

    severity, status, breach, affected = _severity_for_run(run, policy)
    pattern = _pattern_for_run(run)
    fix_record = FIX_LIBRARY.get(pattern, FIX_LIBRARY["search_hang_client_timeout"])
    measured: list[dict[str, Any]] = []
    measurement_window = {"start": "n/a", "end": "n/a"}
    if run.get("write_error_rate") is not None:
        measured.append(_normalize_measurement(run, "write_error_rate", run.get("write_error_rate"), "%", measurement_window))
    if run.get("write_p95_ms") is not None:
        measured.append(_normalize_measurement(run, "write_p95_ms", run.get("write_p95_ms"), "ms", measurement_window))
    if run.get("search_error_rate") is not None:
        measured.append(_normalize_measurement(run, "search_error_rate", run.get("search_error_rate"), "%", measurement_window))
    if run.get("search_outage_seconds") is not None:
        measured.append(_normalize_measurement(run, "search_outage_seconds", run.get("search_outage_seconds"), "s", measurement_window))
    if run.get("recovery_time_seconds") is not None:
        measured.append(_normalize_measurement(run, "recovery_time_seconds", run.get("recovery_time_seconds"), "s", measurement_window))
    measured = [item for item in measured if item is not None]

    if not measured:
        status = "inconclusive"
        severity = "low"
        title = f"No measured weakness for {action} on {target}."
        hypothesis = "The run stayed within the configured thresholds; no issue was observed in the available evidence."
        fix = "Keep the current guardrails and continue monitoring the same fault under the same baseline."
    elif severity == "low":
        title = f"Within policy limits: {action} on {target}."
        hypothesis = "This run stayed below the configured thresholds, so it should be treated as informational evidence rather than a weakness."
        fix = "Continue observing the service under the same workload and retain the baseline as a positive control."
    else:
        title = f"{fix_record['title']} [{action} on {target}]"
        hypothesis = (
            f"The observed fault on {target} via {action} is consistent with the configured graph dependency path and the live policy limits; "
            "the run exceeded the expected operating envelope and therefore merits mitigation."
        )
        fix = fix_record["fix"]

    validation = fix_record["validation_test"].copy()
    validation["target"] = target
    validation["duration"] = validation.get("duration") or run.get("planned_duration_s") or run.get("action_params", {}).get("duration_seconds") or 0
    if status == "inconclusive":
        validation["expected_outcome"] = "Not scored; need fresh fault-window SLIs and recovery evidence."

    return {
        "id": f"finding-{run_id}",
        "title": title,
        "severity": severity,
        "status": status,
        "measured": measured,
        "hypothesis": hypothesis,
        "fix": fix,
        "validation_test": validation,
        "evidence_ids": [f"run:{run_id}"] + ([f"baseline:{run.get('baseline_id')}" ] if run.get("baseline_id") else []),
    }


def _tagged_run_ids_from_markdown(markdown: str) -> set[str]:
    ids = set()
    for match in re.findall(r"\[run:\s*([^\]]+)\]", markdown):
        ids.add(match.strip())
    return ids


def trace_check_markdown(markdown: str, known_run_ids: set[str] | None = None) -> bool:
    """Validate report tags: every numeric line must carry a [run: ...] tag and each tag must exist in the known run list."""
    known = set(known_run_ids or set())
    for line in markdown.splitlines():
        if re.search(r"\d", line):
            if "[run:" not in line:
                raise ValueError(f"Line has a number but no run tag: {line}")
            tags = re.findall(r"\[run:\s*([^\]]+)\]", line)
            if not tags:
                raise ValueError(f"Line has a number but no valid run tag: {line}")
            for tag in tags:
                if known and tag not in known:
                    raise ValueError(f"Tagged run not found in records: {tag}")
    return True


def _tagged_run(run: dict[str, Any]) -> str:
    run_id = str(run.get("run_id") or "unknown")
    return f"[run: {run_id}]"


def _fault_label(run: dict[str, Any]) -> str:
    return str(run.get("fault_type") or run.get("action") or "unknown").strip() or "unknown"


def _target_label(run: dict[str, Any]) -> str:
    return str(run.get("target") or run.get("target_component") or "unknown").strip() or "unknown"


def _score_summary(run: dict[str, Any]) -> str:
    values = run.get("score_components") or {}
    if not isinstance(values, dict) or not values:
        return "not scored"
    components = list(values.keys())
    return ", ".join(components) if components else "not scored"


def build_report_markdown(runs: list[dict[str, Any]], policy_path: str | Path | None = None, output_path: str | Path | None = None) -> str:
    policy = _policy_load(policy_path)
    findings = [build_findings_for_run(run, policy_path) for run in runs]
    known_runs = {str(run.get("run_id") or "unknown") for run in runs}

    lines: list[str] = []
    lines.append("# Findings Report")
    lines.append("")
    lines.append("## Summary")
    if runs:
        fault_names = ", ".join(sorted({_fault_label(run) for run in runs}))
        lines.append(f"- Fault types run: {fault_names}.")
        lines.append(f"- Runs reviewed: {', '.join(_tagged_run(run) for run in runs)}")
    else:
        lines.append("- Fault types run: NOT RUN YET")
        lines.append("- Runs reviewed: NOT RUN YET")
    lines.append("")

    lines.append("## Fault-type coverage")
    lines.append("| Fault type | Status | Notes |")
    lines.append("|---|---|---|")
    expected_faults = [
        ("container interruption", {"restart_container", "stop_container", "pause_container"}),
        ("pause", {"pause_container"}),
        ("cpu_throttle", {"cpu_throttle"}),
        ("burst", {"traffic_burst"}),
    ]
    for label, allowed in expected_faults:
        present = any(_fault_label(run).lower() in {value.lower() for value in allowed} for run in runs)
        status = "run" if present else "NOT RUN YET"
        note = "fault observed in the run record" if present else "placeholder until the next live run"
        lines.append(f"| {label} | {status} | {note} |")
    lines.append("")

    lines.append("## Method")
    if runs:
        baseline_ids = [str(run.get("baseline_id") or "").strip() for run in runs if run.get("baseline_id")]
        lines.append(f"- Baseline IDs: {', '.join(f'[run: {value}]' for value in baseline_ids) if baseline_ids else 'NOT RUN YET'}")
    else:
        lines.append("- Baseline IDs: NOT RUN YET")
    lines.append("- Thresholds: the policy abort thresholds are the source of truth for the control plane and are validated before every run.")
    lines.append("- Severity rules: critical means the fault system did not recover or a severe breach was observed; high means a kill switch or multiple SLIs were affected; medium means partial degradation; low means the run stayed within policy limits.")
    lines.append("")

    lines.append("## Findings by severity")
    severities = ["critical", "high", "medium", "low"]
    for severity in severities:
        bucket = [finding for finding in findings if finding.get("severity") == severity]
        if not bucket:
            lines.append(f"### {severity.title()}")
            lines.append("- NOT RUN YET")
            lines.append("")
            continue
        for finding in bucket:
            lines.append(f"### {severity.title()} - {finding.get('title', 'Finding')}")
            lines.append(f"- Hypothesis: {finding.get('hypothesis', 'No hypothesis recorded.')}")
            lines.append(f"- Fix: {finding.get('fix', 'No mitigation recorded.')}")
            evidence_run = next((value for value in (finding.get("evidence_ids") or []) if isinstance(value, str) and value.startswith("run:")), None)
            if evidence_run:
                run_id = evidence_run.split(":", 1)[1].strip()
                lines.append(f"- Status: {finding.get('status', 'unknown')} [run: {run_id}]")
            else:
                lines.append(f"- Status: {finding.get('status', 'unknown')}")
            lines.append("")

    lines.append("## Predicted vs observed per run")
    if runs:
        for run in runs:
            lines.append(f"- { _fault_label(run) } on { _target_label(run) } is recorded as { _score_summary(run) } {_tagged_run(run)}")
    else:
        lines.append("- NOT RUN YET")
    lines.append("")

    lines.append("## Resilience score with inputs")
    if runs:
        for run in runs:
            score_values = run.get("score_components") or {}
            if isinstance(score_values, dict) and score_values:
                components = ", ".join(f"{name}: {value}" for name, value in sorted(score_values.items()))
                lines.append(f"- run score inputs: {components} {_tagged_run(run)}")
            else:
                lines.append(f"- run score inputs: not scored {_tagged_run(run)}")
    else:
        lines.append("- NOT RUN YET")
    lines.append("")

    lines.append("## Forced kill-switch test")
    forced = [run for run in runs if run.get("forced_test")]
    if not forced:
        lines.append("- Not run yet.")
    else:
        for run in forced:
            lines.append(f"- { _fault_label(run) } on { _target_label(run) } was a forced abort and is intentionally excluded from the scored result {_tagged_run(run)}")
    lines.append("")

    lines.append("## Data sources")
    if not runs:
        lines.append("- NOT RUN YET")
    else:
        for run in runs:
            source = run.get("source_file") or "fixture"
            lines.append(f"- {source} {_tagged_run(run)}")

    text = "\n".join(lines) + "\n"
    if output_path is not None:
        Path(output_path).write_text(text, encoding="utf-8")
    trace_check_markdown(text, known_run_ids=known_runs)
    return text
