"""Deterministic predicted-vs-observed analysis; this module cannot execute faults."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ai.run_record import normalize_run_record

ROOT = Path(__file__).resolve().parents[1]
ORDER = {"hold": 0, "degrade": 1, "fail": 2}


def load_thresholds(policy_path: Path | None = None) -> dict[str, float]:
    policy_file = policy_path or ROOT / "configs" / "experiment-policy.yaml"
    if not policy_file.is_file():
        raise ValueError(f"Experiment policy is missing: {policy_file}")
    policy = yaml.safe_load(policy_file.read_text(encoding="utf-8")) or {}
    aborts = policy.get("abort_thresholds") or {}
    recovery = policy.get("recovery") or {}
    required = {
        "fail_error_rate": aborts.get("write_error_rate"),
        "write_p95_multiplier": aborts.get("write_p95_multiplier"),
        "freshness_p95_seconds": aborts.get("freshness_p95_seconds"),
        "slow_recovery_s": recovery.get("timeout_seconds"),
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ValueError(f"Experiment policy is missing diagnosis limits: {', '.join(missing)}")
    return {name: float(value) for name, value in required.items()}


def classify(error_rate: float | None, p95_ms: float | None, base: dict[str, Any], limits: dict[str, float]) -> str | None:
    base_error = base.get("error_rate")
    base_p95 = base.get("p95_ms")
    if error_rate is not None and error_rate > limits["fail_error_rate"]:
        return "fail"
    if base_p95 and p95_ms is not None and p95_ms > base_p95 * limits["write_p95_multiplier"]:
        return "fail"
    error_degraded = error_rate is not None and base_error is not None and error_rate > base_error
    latency_degraded = p95_ms is not None and base_p95 is not None and p95_ms > base_p95
    if error_degraded or latency_degraded:
        return "degrade"
    if error_rate is not None or p95_ms is not None:
        return "hold"
    return None


def classify_freshness(p95_s: float | None, baseline_p95_s: float | None, abort_s: float) -> str | None:
    if p95_s is None:
        return None
    if p95_s > abort_s:
        return "fail"
    if baseline_p95_s is not None and p95_s > baseline_p95_s:
        return "degrade"
    return "hold"


def _baseline_slis(baseline: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    baseline = baseline or {}
    explicit = baseline.get("slis") or (baseline.get("telemetry") or {}).get("slis")
    if explicit:
        return explicit
    summary = (baseline.get("telemetry") or {}).get("telemetry") or {}
    if not summary:
        return {}
    write = summary.get("write") or {}
    search = summary.get("search") or {}
    freshness = summary.get("freshness") or {}
    return {
        "write": {"error_rate": write.get("error_rate"), "p95_ms": (write.get("latency_ms") or {}).get("p95")},
        "search": {"error_rate": search.get("error_rate"), "p95_ms": (search.get("latency_ms") or {}).get("p95")},
        "freshness": {"error_rate": 0.0, "p95_s": freshness.get("p95_seconds")},
    }


def diagnose(
    experiment: dict[str, Any],
    run: dict[str, Any],
    baseline: dict[str, Any] | None = None,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    normalized = normalize_run_record(run, experiment=experiment)
    limits = thresholds or load_thresholds()
    base_slis = _baseline_slis(baseline)
    predicted = normalized["prediction"].get("predicted_slis") or experiment.get("predicted_slis") or {}
    seen = normalized["sli_windows"]
    warnings: list[str] = []
    if not predicted:
        warnings.append("no predicted_slis on the experiment or run; nothing to compare")
    if normalized.get("experiment_id") not in (None, experiment.get("id")):
        warnings.append(f"run is for experiment {normalized['experiment_id']} but the plan is {experiment.get('id')}")

    comparison: dict[str, Any] = {}
    for sli, expected in predicted.items():
        measurement = seen.get(sli)
        if measurement is None:
            comparison[sli] = {"predicted": expected, "observed": "not_measured", "verdict": "not_measured"}
            continue
        if sli not in base_slis:
            warnings.append(f"no baseline for {sli}; only the absolute error-rate rule was applied")
        if sli == "freshness" and measurement.get("p95_s") is not None:
            actual = classify_freshness(
                measurement.get("p95_s"),
                (base_slis.get("freshness") or {}).get("p95_s"),
                limits["freshness_p95_seconds"],
            )
        elif sli == "freshness" and measurement.get("p95_ms") is None:
            actual = None
        else:
            actual = classify(measurement.get("error_rate"), measurement.get("p95_ms"), base_slis.get(sli, {}), limits)
        if actual is None:
            comparison[sli] = {"predicted": expected, "observed": "not_measured", "verdict": "not_measured"}
            continue
        verdict = "match" if actual == expected else (
            "worse_than_predicted" if ORDER[actual] > ORDER[expected] else "better_than_predicted"
        )
        comparison[sli] = {
            "predicted": expected,
            "observed": actual,
            "verdict": verdict,
            "error_rate": measurement.get("error_rate"),
            "p95_ms": measurement.get("p95_ms"),
        }

    recovery_state = normalized["recovery_state"]
    recovery_status = {
        "recovered": "recovered",
        "not_recovered": "did_not_recover",
        "unknown": "inconclusive",
    }[recovery_state]
    recovery_seconds = normalized["recovery_time_seconds"]
    measured = [result for result in comparison.values() if result["verdict"] != "not_measured"]
    matches = sum(result["verdict"] == "match" for result in measured)
    accuracy = round(matches / len(measured), 2) if measured else None

    forced_test = normalized["forced_test"]
    dry_run = normalized["dry_run"]
    kill_switch_tripped = normalized["kill_switch_tripped"]
    flags: list[str] = []
    if recovery_status == "did_not_recover":
        flags.append("did_not_recover")
    elif recovery_seconds is not None and recovery_seconds > limits["slow_recovery_s"]:
        flags.append("slow_recovery")
    if any(result["verdict"] == "worse_than_predicted" for result in comparison.values()):
        flags.append("worse_than_predicted")
    if kill_switch_tripped:
        flags.append("kill_switch_fired")
    if normalized["safety_violation"]:
        flags.append("safety_violation")
    if normalized["cleanup_state"] not in (None, "complete"):
        flags.append("cleanup_not_complete")
    if (normalized["source"] == "runner_record" and normalized["aborted"]
            and not forced_test and not dry_run):
        flags.append("non_forced_abort")

    if forced_test:
        evidence_use = "kill_switch_test"
    elif dry_run:
        evidence_use = "dry_run"
    elif (not measured or any(result["verdict"] == "not_measured" for result in comparison.values())
          or recovery_state == "unknown" or normalized["cleanup_state"] != "complete"
          or normalized["safety_violation"] or normalized["status"] in {"aborted", "failed"}):
        evidence_use = "inconclusive"
    else:
        evidence_use = "experiment"

    target = experiment.get("target_component") or normalized["target"]
    action = experiment.get("action") or normalized["fault_type"]
    summary = f"{target} {action}: "
    if evidence_use == "dry_run":
        summary += "dry run (no container was changed; not experiment evidence)."
    elif evidence_use == "kill_switch_test":
        summary += "forced kill-switch test (proves the automatic abort; not used as experiment evidence)."
    elif not measured:
        summary += "no SLI was measured during the run, so the prediction could not be checked."
    else:
        summary += f"prediction matched on {matches} of {len(measured)} measured SLIs."
    if recovery_state == "recovered" and recovery_seconds is not None:
        summary += f" Recovery: {recovery_seconds}s."
    elif recovery_state == "not_recovered":
        summary += " Did not recover in time."
    else:
        summary += " Recovery evidence is missing; status is inconclusive."
    missing = [name for name, result in comparison.items() if result["verdict"] == "not_measured"]
    if missing and measured:
        summary += f" Not measured: {', '.join(missing)}."

    return {
        "schema_version": "1.0",
        "diagnosis_id": f"diag-{normalized['run_id']}",
        "run_id": normalized["run_id"],
        "experiment_id": experiment.get("id") or normalized.get("experiment_id"),
        "target_component": target,
        "action": action,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "comparison": comparison,
        "prediction_accuracy": accuracy,
        "recovery_status": recovery_status,
        "recovered": recovery_state == "recovered",
        "recovery_s": recovery_seconds,
        "recovery_seconds": recovery_seconds,
        "kill_switch": {"tripped": kill_switch_tripped, "forced_test": forced_test},
        "evidence_use": evidence_use,
        "flags": flags,
        "warnings": warnings,
        "summary": summary,
    }


def history_outcome(diagnosis: dict[str, Any]) -> str | None:
    """Only real mismatches, non-forced aborts, and recovery failures weaken a target."""
    if diagnosis.get("kill_switch", {}).get("forced_test") or diagnosis["evidence_use"] in {"kill_switch_test", "dry_run"}:
        return None
    flags = set(diagnosis.get("flags", []))
    mismatched = any(result["verdict"] == "worse_than_predicted" for result in diagnosis["comparison"].values())
    if mismatched or flags.intersection({"non_forced_abort", "did_not_recover", "slow_recovery"}):
        return "failure_observed"
    if diagnosis["evidence_use"] != "experiment":
        return None
    return "no_failure_observed"


def _print(diagnosis: dict[str, Any]) -> None:
    print(diagnosis["summary"])
    for sli, result in diagnosis["comparison"].items():
        print(f"  {sli:10} predicted={result['predicted']:8} observed={result['observed']:13} {result['verdict']}")
    if diagnosis["flags"]:
        print("Flags:", ", ".join(diagnosis["flags"]))
    for warning in diagnosis["warnings"]:
        print("Warning:", warning)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare a planned prediction with one run's observations.")
    parser.add_argument("--experiment", required=True, help="candidate JSON from the planner")
    parser.add_argument("--run", required=True, help="run record JSON from the control plane")
    parser.add_argument("--baseline", help="baseline JSON with an 'slis' object")
    parser.add_argument("--out", default=str(ROOT / "data" / "diagnoses"))
    parser.add_argument("--record-history", action="store_true", help="record only conclusive real outcomes")
    args = parser.parse_args()
    load = lambda path: json.loads(Path(path).read_text(encoding="utf-8"))
    experiment, raw_run = load(args.experiment), load(args.run)
    if "candidates" in experiment:
        experiment = next(
            (candidate for candidate in experiment["candidates"] if candidate["id"] == raw_run.get("experiment_id")),
            experiment["candidates"][0],
        )
    sys.path.insert(0, str(ROOT / "backend"))
    from app.settings import Settings

    settings = Settings.from_environment()
    run = normalize_run_record(raw_run, experiment=experiment, run_path=args.run, metrics_dir=settings.metrics_dir)
    diagnosis = diagnose(experiment, run, load(args.baseline) if args.baseline else None)
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{diagnosis['diagnosis_id']}.json"
    path.write_text(json.dumps(diagnosis, indent=2), encoding="utf-8")
    _print(diagnosis)
    print("Saved:", path)
    if args.record_history:
        outcome = history_outcome(diagnosis)
        if outcome is None:
            print("History: not recorded (", diagnosis["evidence_use"], ")")
        else:
            from app.store import LocalRepository

            LocalRepository(settings.database_path).record_outcome(
                diagnosis["target_component"], diagnosis["action"], outcome, f"run:{diagnosis['run_id']}"
            )
            print("History: recorded", outcome)


if __name__ == "__main__":
    main()
