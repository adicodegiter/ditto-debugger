from __future__ import annotations

from typing import Any


def generate_findings(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate deterministic, evidence-linked findings without an external model."""
    diagnosis = run.get("diagnosis") or {}
    if (
        run.get("dry_run") is True
        or run.get("forced_test") is True
        or diagnosis.get("kill_switch", {}).get("forced_test") is True
        or diagnosis.get("evidence_use") in {"dry_run", "kill_switch_test"}
    ):
        return []
    run_id = str(run["run_id"])
    observations = run.get("observations", [])
    evidence = [f"run:{run_id}"]
    if run.get("abort_reason"):
        evidence.append(f"abort:{run['abort_reason']}")
    if run.get("recovery"):
        evidence.append(f"recovery:{run['recovery'].get('target', 'target')}")
    findings: list[dict[str, Any]] = []
    # Real runner windows use write_error_rate / search_error_rate; older records use error_rate.
    error_keys = ("error_rate", "write_error_rate", "search_error_rate")
    # Real windows carry a phase; only fault-phase windows count. Older records have no phase.
    failed_samples = sum(
        1 for observation in observations
        if (observation.get("phase", "fault") == "fault"
            and any(float(observation.get(key, 0) or 0) > 0 for key in error_keys))
        or observation.get("telemetry_available") is False
    )
    if failed_samples:
        findings.append({
            "schema_version": "1.0",
            "id": f"{run_id}-availability",
            "run_id": run_id,
            "severity": "high" if run.get("abort_reason") else "medium",
            "summary": "The approved experiment caused measurable client-side availability degradation.",
            "evidence": evidence + [f"observation_count:{len(observations)}", f"degraded_observations:{failed_samples}"],
            "recommendation": "Investigate the affected dependency and add an availability budget or timeout protection before increasing fault duration.",
            "confidence": 0.95,
            "ai_generated": False,
        })
    if run.get("recovery_verified") is False:
        findings.append({
            "schema_version": "1.0",
            "id": f"{run_id}-recovery",
            "run_id": run_id,
            "severity": "critical",
            "summary": "The target did not pass the authenticated health and functional recovery checks within the recovery window.",
            "evidence": evidence + ["recovery_verified:false"],
            "recommendation": "Review service startup dependencies and extend recovery automation only after confirming the functional canary remains bounded and local.",
            "confidence": 0.99,
            "ai_generated": False,
        })
    if diagnosis.get("evidence_use") == "experiment":
        worse = {sli: v for sli, v in diagnosis.get("comparison", {}).items() if v.get("verdict") == "worse_than_predicted"}
        better = {sli: v for sli, v in diagnosis.get("comparison", {}).items() if v.get("verdict") == "better_than_predicted"}
        if worse:
            findings.append({
                "schema_version": "1.0",
                "id": f"{run_id}-worse-than-predicted",
                "run_id": run_id,
                "severity": "high",
                "summary": "Impact was worse than predicted on: " + ", ".join(f"{sli} (predicted {v['predicted']}, observed {v['observed']})" for sli, v in worse.items()) + ".",
                "evidence": evidence + [f"diagnosis:{diagnosis.get('diagnosis_id')}"] + [f"{sli}:{v['observed']}" for sli, v in worse.items()],
                "recommendation": "Treat this as an unexpected coupling: check timeouts, retries and resource limits on the affected path before widening fault scope.",
                "confidence": 0.9,
                "ai_generated": False,
            })
        elif better:
            kill_switch_trip = bool(diagnosis.get("kill_switch", {}).get("tripped")) or bool(run.get("abort_reason"))
            severity = "high" if kill_switch_trip else "info"
            findings.append({
                "schema_version": "1.0",
                "id": f"{run_id}-better-than-predicted",
                "run_id": run_id,
                "severity": severity,
                "summary": "The system absorbed more than predicted on: " + ", ".join(f"{sli} (predicted {v['predicted']}, observed {v['observed']})" for sli, v in better.items()) + ".",
                "evidence": evidence + [f"diagnosis:{diagnosis.get('diagnosis_id')}"],
                "recommendation": "Update the planner's effect model, or check that the measurement window captured the full outage.",
                "confidence": 0.75,
                "ai_generated": False,
            })
    if not findings:
        findings.append({
            "schema_version": "1.0",
            "id": f"{run_id}-absorbed",
            "run_id": run_id,
            "severity": "info",
            "summary": "The target absorbed the approved fault without a measured client-side SLI breach.",
            "evidence": evidence + ["degraded_observations:0"],
            "recommendation": "Repeat with the same workload and compare the result before increasing fault scope or duration.",
            "confidence": 0.85,
            "ai_generated": False,
        })
    return findings
