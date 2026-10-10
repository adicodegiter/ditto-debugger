"""P4 diagnosis (Day 1): compare what the planner predicted with what one run observed.

Deterministic. No LLM, no execution capability. Reads three records:
  experiment : a candidate from the planner (schemas/experiment.schema.json), with predicted_slis
  run        : the run record from the control plane (schemas/experiment-run.schema.json)
  baseline   : {"slis": {"write": {"error_rate": 0.0, "p95_ms": 35.0}, ...}} from the telemetry agent

Observation conventions inside run["observations"] (free-form objects in the run schema):
  {"kind": "sli", "sli": "search", "window": "during", "error_rate": 1.0, "p95_ms": 3000}
  {"kind": "recovery", "recovery_s": 38.0, "recovered": true}
  {"kind": "kill_switch", "tripped": true, "forced_test": false, "reason": "..."}
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
ORDER = {"hold": 0, "degrade": 1, "fail": 2}
DEFAULTS = {"fail_error_rate": 0.20, "degrade_error_rise": 0.02, "slow_recovery_s": 60}


def load_thresholds(path: Path | None = None) -> dict[str, float]:
    thresholds = dict(DEFAULTS)
    path = path or ROOT / "configs" / "planner.yaml"
    if Path(path).is_file():
        thresholds.update((yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}).get("diagnosis", {}))
    return thresholds


def classify(error_rate: float, p95_ms: float | None, base: dict[str, Any], t: dict[str, float]) -> str:
    base_err = base.get("error_rate") or 0.0
    base_p95 = base.get("p95_ms") or 0.0
    if error_rate >= t["fail_error_rate"]:
        return "fail"
    slow = bool(base_p95) and (p95_ms or 0.0) > max(2 * base_p95, base_p95 + 100)
    if error_rate > base_err + t["degrade_error_rise"] or slow:
        return "degrade"
    return "hold"


def _during_sli_observations(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    worst: dict[str, dict[str, Any]] = {}
    for item in run.get("observations", []):
        if item.get("kind") != "sli" or item.get("window", "during") != "during":
            continue
        sli = item.get("sli")
        if sli and (sli not in worst or item.get("error_rate", 0.0) > worst[sli].get("error_rate", 0.0)):
            worst[sli] = item
    return worst


def diagnose(experiment: dict[str, Any], run: dict[str, Any], baseline: dict[str, Any] | None = None,
             thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    t = thresholds or load_thresholds()
    base_slis = ((baseline or {}).get("slis")) or ((baseline or {}).get("telemetry", {}).get("slis")) or {}
    predicted = (run.get("prediction") or {}).get("predicted_slis") or experiment.get("predicted_slis") or {}
    seen = _during_sli_observations(run)
    warnings: list[str] = []
    if not predicted:
        warnings.append("no predicted_slis on the experiment or run; nothing to compare")
    if run.get("experiment_id") != experiment.get("id"):
        warnings.append(f"run is for experiment {run.get('experiment_id')} but the plan is {experiment.get('id')}")

    comparison: dict[str, Any] = {}
    for sli, expected in predicted.items():
        if sli not in seen:
            comparison[sli] = {"predicted": expected, "observed": "not_measured", "verdict": "not_measured"}
            continue
        if sli not in base_slis:
            warnings.append(f"no baseline for {sli}; only the absolute error-rate rule was applied")
        m = seen[sli]
        observed = classify(m.get("error_rate", 0.0), m.get("p95_ms"), base_slis.get(sli, {}), t)
        verdict = "match" if observed == expected else ("worse_than_predicted" if ORDER[observed] > ORDER[expected] else "better_than_predicted")
        comparison[sli] = {"predicted": expected, "observed": observed, "verdict": verdict,
                           "error_rate": m.get("error_rate"), "p95_ms": m.get("p95_ms")}

    recovery = next((o for o in run.get("observations", []) if o.get("kind") == "recovery"), {})
    kill = next((o for o in run.get("observations", []) if o.get("kind") == "kill_switch"), {})
    rec_s = recovery.get("recovery_s")
    recovered = recovery.get("recovered", rec_s is not None)

    measured = [v for v in comparison.values() if v["verdict"] != "not_measured"]
    matches = sum(1 for v in measured if v["verdict"] == "match")
    accuracy = round(matches / len(measured), 2) if measured else None

    flags: list[str] = []
    if not recovered:
        flags.append("did_not_recover")
    elif rec_s is not None and rec_s > t["slow_recovery_s"]:
        flags.append("slow_recovery")
    if any(v["verdict"] == "worse_than_predicted" for v in comparison.values()):
        flags.append("worse_than_predicted")
    if kill.get("tripped"):
        flags.append("kill_switch_fired")
    if run.get("safety_violation"):
        flags.append("safety_violation")
    if run.get("cleanup_state") not in (None, "complete"):
        flags.append("cleanup_not_complete")

    if kill.get("forced_test"):
        evidence_use = "kill_switch_test"
    elif not measured or run.get("safety_violation") or run.get("status") == "failed":
        evidence_use = "inconclusive"
    else:
        evidence_use = "experiment"

    target = experiment.get("target_component")
    summary = f"{target} {experiment.get('action')}: "
    if evidence_use == "kill_switch_test":
        summary += "forced kill-switch test (proves the automatic abort; not used as experiment evidence)."
    elif not measured:
        summary += "no SLI was measured during the run, so the prediction could not be checked."
    else:
        summary += f"prediction matched on {matches} of {len(measured)} measured SLIs."
    if recovered and rec_s is not None:
        summary += f" Recovery: {rec_s}s."
    elif not recovered:
        summary += " Did not recover in time."
    if kill.get("tripped") and evidence_use != "kill_switch_test":
        summary += " Kill switch fired."
    missing = [s for s, v in comparison.items() if v["verdict"] == "not_measured"]
    if missing and measured:
        summary += f" Not measured: {', '.join(missing)}."

    return {
        "schema_version": "1.0",
        "diagnosis_id": f"diag-{run.get('run_id')}",
        "run_id": run.get("run_id"),
        "experiment_id": experiment.get("id"),
        "target_component": target,
        "action": experiment.get("action"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "comparison": comparison,
        "prediction_accuracy": accuracy,
        "recovered": bool(recovered),
        "recovery_s": rec_s,
        "kill_switch": {"tripped": bool(kill.get("tripped")), "forced_test": bool(kill.get("forced_test"))},
        "evidence_use": evidence_use,
        "flags": flags,
        "warnings": warnings,
        "summary": summary,
    }


def history_outcome(diagnosis: dict[str, Any]) -> str | None:
    """Outcome to store in planner history, or None when the run must not influence ranking
    (forced kill-switch tests and runs with a telemetry gap are not resilience observations)."""
    if diagnosis["evidence_use"] != "experiment":
        return None
    hurt = any(v["observed"] in ("fail", "degrade") for v in diagnosis["comparison"].values())
    return "failure_observed" if (hurt or not diagnosis["recovered"]) else "no_failure_observed"


def _print(d: dict[str, Any]) -> None:
    print(d["summary"])
    for sli, v in d["comparison"].items():
        print(f"  {sli:10} predicted={v['predicted']:8} observed={v['observed']:13} {v['verdict']}")
    if d["flags"]:
        print("Flags:", ", ".join(d["flags"]))
    for w in d["warnings"]:
        print("Warning:", w)


def main() -> None:
    ap = argparse.ArgumentParser(description="Compare a planned prediction with one run's observations.")
    ap.add_argument("--experiment", required=True, help="candidate JSON from the planner")
    ap.add_argument("--run", required=True, help="run record JSON from the control plane")
    ap.add_argument("--baseline", help="baseline JSON with an 'slis' object")
    ap.add_argument("--out", default=str(ROOT / "data" / "diagnoses"))
    ap.add_argument("--record-history", action="store_true",
                    help="store the outcome in the planner history (skipped for forced tests / telemetry gaps)")
    a = ap.parse_args()
    load = lambda p: json.loads(Path(p).read_text(encoding="utf-8"))
    experiment, run = load(a.experiment), load(a.run)
    if "candidates" in experiment:  # a whole plan file: pick the candidate this run executed
        experiment = next((c for c in experiment["candidates"] if c["id"] == run.get("experiment_id")), experiment["candidates"][0])
    d = diagnose(experiment, run, load(a.baseline) if a.baseline else None)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{d['diagnosis_id']}.json"
    path.write_text(json.dumps(d, indent=2), encoding="utf-8")
    _print(d)
    print("Saved:", path)
    if a.record_history:
        outcome = history_outcome(d)
        if outcome is None:
            print("History: not recorded (", d["evidence_use"], ")")
        else:
            sys.path.insert(0, str(ROOT / "backend"))
            from app.settings import Settings
            from app.store import LocalRepository
            LocalRepository(Settings.from_environment().database_path).record_outcome(
                d["target_component"], d["action"], outcome, f"run:{d['run_id']}")
            print("History: recorded", outcome)


if __name__ == "__main__":
    main()
