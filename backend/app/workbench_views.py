"""Read-only views for the dashboard: live SLIs, policy, dependency map, watchdog log.

Everything here only reads local files (data/metrics, configs, ai/, data/experiments).
Nothing here executes or changes anything.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from typing import Any

from safety.observations import _records, percentile, summarize_window
from safety.policy import Policy

METRIC_FILES = ("write.jsonl", "search.jsonl", "freshness.jsonl")


def _parse(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _tail_records(metrics_dir: Path, since: float) -> list[dict[str, Any]]:
    """Records newer than ``since`` (reads the files; fine for hackathon-sized JSONL)."""
    paths = [Path(metrics_dir) / name for name in METRIC_FILES]
    output = []
    for record in _records(paths):
        ts = _parse(record.get("timestamp", record.get("ts")))
        if ts is not None and ts >= since:
            output.append(record)
    return output


def sli_window(metrics_dir: Path, window_seconds: float = 30.0, now: float | None = None) -> dict[str, Any]:
    end = now or time.time()
    start = end - window_seconds
    records = _tail_records(metrics_dir, start)
    summary = summarize_window(records, start, end)
    latest = max((_parse(r.get("timestamp", r.get("ts"))) or 0 for r in records), default=None)
    methods = sorted({str(r.get("method")) for r in records if r.get("kind") == "freshness" and r.get("method")})
    # True only when every record in the window is simulated: the samples are flowing, but no
    # live Ditto is involved. The dashboard must not call simulated data "Live telemetry".
    simulated = bool(records) and {str(r.get("source", "live")) for r in records} == {"simulated"}
    return {
        **summary,
        "window_seconds": window_seconds,
        "window_start": _iso(start),
        "window_end": _iso(end),
        "latest_sample_at": _iso(latest) if latest else None,
        "freshness_method": methods[0] if len(methods) == 1 else ("mixed" if methods else None),
        "live": bool(latest and end - latest < 15) and not simulated,
        "simulated": simulated,
    }


def sli_series(metrics_dir: Path, minutes: float = 5.0, bucket_seconds: float = 10.0, now: float | None = None) -> dict[str, Any]:
    end = now or time.time()
    start = end - minutes * 60
    records = _tail_records(metrics_dir, start)
    buckets = max(1, int(math.ceil((end - start) / bucket_seconds)))
    series: list[dict[str, Any]] = []
    grouped: list[list[dict[str, Any]]] = [[] for _ in range(buckets)]
    for record in records:
        ts = _parse(record.get("timestamp", record.get("ts")))
        if ts is None:
            continue
        index = min(buckets - 1, int((ts - start) // bucket_seconds))
        if index >= 0:
            grouped[index].append(record)
    for index, items in enumerate(grouped):
        bucket_start = start + index * bucket_seconds
        summary = summarize_window(items, bucket_start, bucket_start + bucket_seconds) if items else None
        series.append({
            "t": _iso(bucket_start),
            "write_samples": summary["write_samples"] if summary else 0,
            "write_success": (1 - summary["write_error_rate"]) if summary and summary["write_error_rate"] is not None else None,
            "write_p95_ms": summary["write_p95_ms"] if summary else None,
            "search_success": (1 - summary["search_error_rate"]) if summary and summary["search_error_rate"] is not None else None,
            "freshness_p95_seconds": summary["freshness_p95_seconds"] if summary else None,
        })
    return {"minutes": minutes, "bucket_seconds": bucket_seconds, "points": series}


def policy_summary(policy_path: Path, execution_enabled: bool) -> dict[str, Any]:
    policy = Policy.from_file(policy_path)
    thresholds = policy.thresholds
    return {
        "compose_project": policy.compose_project,
        "allowed_targets": sorted(policy.allowed_targets),
        "denied_targets": sorted(policy.denied_targets),
        "enabled_actions": sorted(policy.enabled_actions),
        "max_duration_seconds": policy.max_duration_seconds,
        "max_risk_score": policy.max_risk_score,
        "approval_required": policy.approval_required,
        "approval_expires_after_minutes": policy.approval_expires_after_minutes,
        "baseline_max_age_seconds": policy.baseline_max_age_seconds,
        "abort_thresholds": {
            "window_seconds": thresholds.window_seconds,
            "poll_interval_seconds": thresholds.poll_interval_seconds,
            "write_error_rate": thresholds.write_error_rate,
            "write_p95_multiplier": thresholds.write_p95_multiplier,
            "freshness_p95_seconds": thresholds.freshness_p95_seconds,
            "min_write_samples": thresholds.min_write_samples,
            "min_freshness_samples": thresholds.min_freshness_samples,
        },
        "recovery_timeout_seconds": policy.recovery_timeout_seconds,
        "watchdog_grace_seconds": policy.watchdog_grace_seconds,
        "operator_execution_enabled": execution_enabled,
        "api_executes": False,
    }


def dependency_map(path: Path) -> dict[str, Any]:
    if not Path(path).is_file():
        return {"status": "unavailable", "nodes": [], "edges": [], "warnings": ["Run python -m ai.dependency_map to generate it."]}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def watchdog_log(path: Path, limit: int = 50) -> list[dict[str, Any]]:
    if not Path(path).is_file():
        return []
    lines = Path(path).read_text(encoding="utf-8").splitlines()[-limit:]
    events = []
    for line in reversed(lines):
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def baseline_view(baseline: dict[str, Any]) -> dict[str, Any]:
    telemetry = baseline.get("telemetry") or {}
    summary = telemetry.get("telemetry") or {}
    write = summary.get("write") or {}
    search = summary.get("search") or {}
    freshness = summary.get("freshness") or {}
    return {
        "baseline_id": baseline.get("baseline_id"),
        "captured_at": baseline.get("captured_at"),
        "graph_version": baseline.get("graph_version"),
        "telemetry_status": baseline.get("telemetry_status"),
        # Present only for baselines captured from WORKLOAD_MODE=simulated samples; the dashboard
        # must be able to label them as simulated, never as live telemetry.
        "sample_source": telemetry.get("sample_source"),
        "run_id": telemetry.get("run_id"),
        "window": telemetry.get("window"),
        "write_samples": write.get("samples"),
        "write_error_rate": write.get("error_rate"),
        "write_p95_ms": (write.get("latency_ms") or {}).get("p95"),
        "search_error_rate": search.get("error_rate"),
        "freshness_p95_seconds": freshness.get("p95_seconds"),
        "freshness_method": freshness.get("method"),
    }


def percentile_of(values: list[float], fraction: float) -> float | None:
    return percentile(values, fraction)


# ---------------------------------------------------------------- stretch goals (read-only)
# Combined failures and re-run-after-fix are judged from stored run records and their raw probe
# samples. Nothing below runs Docker or touches a container: the "is a fix live" answer is the
# deployment stamp the operator CLI recorded on the last run, never a live `nginx -T` from the API.

def _is_real(run: dict[str, Any]) -> bool:
    return not run.get("dry_run") and not run.get("forced_test")


def run_interaction(run: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Recompute a combined run's interaction verdict from the solo runs stored now (never stale)."""
    from ai.compound import interaction_effect

    if run.get("kind") != "compound":
        return {"status": "not_compound", "run_id": run.get("run_id")}
    def fixes(item: dict[str, Any]) -> frozenset[str]:
        return frozenset((item.get("deployment") or {}).get("remediations_active") or [])

    # A solo run only counts if the same fixes were live (a fix changes how the fault behaves) and it
    # ran before the combined run.
    solos = [item for item in runs if item.get("kind") != "compound" and item.get("run_id") != run.get("run_id")
             and fixes(item) == fixes(run) and str(item.get("created_at", "")) <= str(run.get("created_at", ""))]
    effect = interaction_effect(run, solos)
    faults = [{"action": f.get("action"), "target": f.get("target"), "start_offset_seconds": f.get("start_offset_seconds"),
               "duration_seconds": f.get("duration_seconds"), "state": f.get("state")} for f in run.get("faults") or []]
    return {"status": "available", **effect, "faults": faults, "stored_verdict": (run.get("interaction") or {}).get("verdict")}


def _evidence_run(repo_root: Path, run_id: str) -> dict[str, Any] | None:
    for path in sorted((repo_root / "docs" / "evidence").glob(f"**/{run_id}.json")):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _run_samples(repo_root: Path, metrics_dir: Path, run: dict[str, Any]) -> list[dict[str, Any]]:
    """Committed samples next to the evidence first, then the live metrics files."""
    from ai.request_evidence import extract_samples, load_samples

    for path in sorted((repo_root / "docs" / "evidence").glob(f"**/samples/{run.get('run_id')}.jsonl")):
        return load_samples(path)
    return extract_samples(run, metrics_dir)


def remediation_views(repo_root: Path, metrics_dir: Path, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Each registered fix, the latest before/after pair for it, and its verdict (ai.remediation.verify)."""
    from ai.ranking import _target_and_action
    from ai.remediation import load_registry, verify

    try:
        registry = load_registry(repo_root / "configs" / "remediations.yaml")
    except (OSError, ValueError) as error:
        return {"remediations": [], "warnings": [f"registry unreadable: {type(error).__name__}"]}
    ordered = sorted((r for r in runs if _is_real(r)), key=lambda r: str(r.get("created_at", "")))
    stamped = [r for r in ordered if isinstance((r.get("deployment") or {}).get("remediations_active"), list)]
    latest_stamp = stamped[-1] if stamped else None
    out = []
    for name, entry in registry.items():
        want = entry.get("validate_with") or {}
        pair: tuple[dict[str, Any], dict[str, Any]] | None = None
        if entry.get("kind") == "server":
            same = [r for r in stamped if _target_and_action(r) == (want.get("target"), want.get("action"))]
            afters = [r for r in same if name in r["deployment"]["remediations_active"]]
            if afters:
                after = afters[-1]
                befores = [r for r in same if name not in r["deployment"]["remediations_active"]
                           and str(r.get("created_at", "")) < str(after.get("created_at", ""))]
                if befores:
                    pair = (befores[-1], after)
        else:
            refs = entry.get("evidence") or {}
            by_id = {r.get("run_id"): r for r in runs}
            before = by_id.get(refs.get("before")) or _evidence_run(repo_root, str(refs.get("before")))
            after = by_id.get(refs.get("after")) or _evidence_run(repo_root, str(refs.get("after")))
            if before and after:
                pair = (before, after)
        item: dict[str, Any] = {
            "name": name, "kind": entry.get("kind"), "addresses": entry.get("addresses"), "change": entry.get("change"),
            "validate_with": want,
            "live_in_last_recorded_run": (name in latest_stamp["deployment"]["remediations_active"]) if latest_stamp else None,
            "last_recorded_run": (latest_stamp or {}).get("run_id"),
            "last_recorded_at": ((latest_stamp or {}).get("deployment") or {}).get("checked_at"),
            "pair": None,
        }
        if pair:
            try:
                result = verify(name, pair[0], pair[1], _run_samples(repo_root, metrics_dir, pair[0]),
                                _run_samples(repo_root, metrics_dir, pair[1]), registry)
                facts = result["request_facts"]
                item["pair"] = {
                    "before_run_id": result["before_run_id"], "after_run_id": result["after_run_id"],
                    "verdict": result["verdict"],
                    "checks": [{k: c.get(k) for k in ("check", "status", "detail")} for g in ("conditions", "success_criteria", "guards") for c in result[g]],
                    "before": {k: facts["before"].get(k) for k in ("search_failures", "search_failure_errors", "search_failure_wait_seconds")},
                    "after": {k: facts["after"].get(k) for k in ("search_failures", "search_failure_errors", "search_failure_wait_seconds")},
                }
            except Exception as error:  # noqa: BLE001 - a broken pair is shown as unavailable, never as a pass
                item["pair"] = {"verdict": "unavailable", "reason": type(error).__name__}
        out.append(item)
    return {"remediations": out, "api_reads_docker": False}
