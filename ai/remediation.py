"""Stretch goal: re-run after applying a fix, and show whether the fix actually worked.

A fix (configs/remediations.yaml) is judged on a before/after pair of REAL runs of the same
experiment, using only measured evidence: the run records plus their raw probe samples
(ai.request_evidence). The verdict is

  verified       every condition, success criterion and no-regression guard passed
  not_verified   a criterion or guard failed on measured data
  inconclusive   evidence is missing (never counted as a pass)

Deterministic and read-only: no execution, no network, no LLM.

    python -m ai.remediation verify search-fail-fast --before BEFORE.json --after AFTER.json
    python -m ai.remediation verify search-client-timeout --evidence      # committed 5.7 records
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from .ranking import _target_and_action
from .request_evidence import extract_samples, load_samples, request_facts
from .retest import compare_retest_runs
from .run_record import normalize_run_record

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "configs" / "remediations.yaml"
EVIDENCE = ROOT / "docs" / "evidence" / "windows-host"

PASS, FAIL, MISSING = "pass", "fail", "not_measured"


def load_registry(path: Path = REGISTRY) -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return raw.get("remediations") or {}


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _check(name: str, status: str, detail: str, **values: Any) -> dict[str, Any]:
    return {"check": name, "status": status, "detail": detail, **values}


def _active(run: dict[str, Any]) -> list[str] | None:
    deployment = run.get("deployment")
    if not isinstance(deployment, dict) or not isinstance(deployment.get("remediations_active"), list):
        return None
    return list(deployment["remediations_active"])


def _enrich(run: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    # Older records (M4) carry action/target only in the experiment id and recovery check;
    # ai.ranking already decodes both formats.
    target, action = _target_and_action(run)
    reported = {**(run.get("_reported_results") or {}), **{k: v for k, v in facts.items() if v is not None}}
    return {**run, "_reported_results": reported,
            "action": run.get("action") or (action if action != "unknown" else None),
            "target": run.get("target") or (target if target != "unknown" else None)}


def _fault_seconds(run: dict[str, Any]) -> tuple[float | None, str]:
    """Planned duration if recorded, else measured from the timeline (injection -> rollback)."""
    planned = _number((run.get("action_params") or {}).get("duration_seconds"))
    if planned is not None:
        return planned, "planned"
    timeline = run.get("timeline") or {}
    try:
        from datetime import datetime
        start = datetime.fromisoformat(str(timeline["injected_at"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(timeline["rollback_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None, "missing"
    return round((end - start).total_seconds(), 1), "measured from timeline"


def _worst(run: dict[str, Any], key: str) -> float | None:
    values = [_number(item.get(key)) for item in run.get("observations", []) if isinstance(item, dict) and item.get("phase") == "fault"]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def trim_to_experiment(run: dict[str, Any], samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Keep samples up to the moment recovery was verified: after that the experiment is over.

    Anything later (e.g. a fix deploy right after the run briefly dropping connections) is not
    caused by the fault and must not be counted for or against the fix.
    """
    timeline = run.get("timeline") or {}
    end = timeline.get("recovered_at")
    if not end:
        return samples, 0
    kept = [item for item in samples if str(item.get("timestamp", "")) <= _z(end)]
    return kept, len(samples) - len(kept)


def _z(stamp: str) -> str:
    """Comparable ISO-8601 UTC text (samples end in 'Z' with milliseconds)."""
    from datetime import datetime, timezone
    parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"


MIN_FAULT_WRITES = 10


def _fault_write_p95(run: dict[str, Any], samples: list[dict[str, Any]]) -> tuple[float | None, str]:
    """Write p95 (ms) of successful writes completed between injection and rollback."""
    from datetime import datetime
    timeline = run.get("timeline") or {}
    try:
        start = datetime.fromisoformat(str(timeline["injected_at"]).replace("Z", "+00:00")).timestamp()
        end = datetime.fromisoformat(str(timeline["rollback_at"]).replace("Z", "+00:00")).timestamp()
        latencies = sorted(
            float(item["latency_ms"]) for item in samples
            if item.get("kind") == "write" and item.get("success") is True and _number(item.get("latency_ms")) is not None
            and start <= datetime.fromisoformat(str(item["timestamp"]).replace("Z", "+00:00")).timestamp() <= end
        )
    except (KeyError, ValueError):
        latencies = []
    if len(latencies) >= MIN_FAULT_WRITES:
        index = max(0, min(len(latencies) - 1, -(-95 * len(latencies) // 100) - 1))
        return latencies[index], f"raw samples in fault window (n={len(latencies)})"
    worst = _worst(run, "write_p95_ms")
    return worst, "worst kill-switch window (too few raw samples)" if worst is not None else "missing"


def verify(name: str, before: dict[str, Any], after: dict[str, Any],
           before_samples: list[dict[str, Any]], after_samples: list[dict[str, Any]],
           registry: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    registry = registry if registry is not None else load_registry()
    if name not in registry:
        raise ValueError(f"Unknown remediation '{name}'.")
    entry = registry[name]
    before_samples, dropped_before = trim_to_experiment(before, before_samples)
    after_samples, dropped_after = trim_to_experiment(after, after_samples)
    before_facts = request_facts(before, before_samples)
    after_facts = request_facts(after, after_samples)
    retest = compare_retest_runs(_enrich(before, before_facts), _enrich(after, after_facts))
    nb, na = normalize_run_record(before), normalize_run_record(after)
    conditions, success, guards = [], [], []

    # --- conditions: a fair, real, like-for-like pair --------------------------------------
    for label, run in (("before", nb), ("after", na)):
        real = not run["dry_run"] and not run["forced_test"]
        conditions.append(_check(f"{label} run is real evidence", PASS if real else FAIL,
                                 "dry runs and forced kill-switch tests never count", run_id=run["run_id"]))
    expected = entry.get("validate_with") or {}
    for key, field in (("action", "same_fault"), ("target", "same_target")):
        want = expected.get(key)
        seen = {retest_side.get(key) for retest_side in (_enrich(before, {}), _enrich(after, {}))}
        if retest["conditions"].get(field) is None:
            conditions.append(_check(f"same {key}", MISSING, "not recorded on both runs"))
        elif not retest["conditions"][field] or (want and seen != {want}):
            conditions.append(_check(f"same {key}", FAIL, f"both runs must be {want or 'the same'}", seen=sorted(map(str, seen))))
        else:
            conditions.append(_check(f"same {key}", PASS, f"both runs: {want or seen.pop()}"))
    (db, sb), (da, sa) = _fault_seconds(before), _fault_seconds(after)
    if db is None or da is None:
        conditions.append(_check("same duration", MISSING, "fault duration not recorded on both runs"))
    else:
        # Measured windows include the command time (a restart takes seconds), so allow some slack.
        close = abs(db - da) <= max(2.0, 0.2 * max(db, da))
        conditions.append(_check("same duration", PASS if close else FAIL,
                                 "fault must last about as long in both runs (within 2 s or 20%)",
                                 before_seconds=db, before_source=sb, after_seconds=da, after_source=sa))
    if entry.get("kind") == "server":
        active_before, active_after = _active(before), _active(after)
        if active_before is None or active_after is None:
            conditions.append(_check("fix active only in the after run", MISSING,
                                     "run record has no deployment.remediations_active (recorded by safety.cli execute)"))
        else:
            ok = name not in active_before and name in active_after
            conditions.append(_check("fix active only in the after run", PASS if ok else FAIL,
                                     "read from the running nginx config when each run executed",
                                     before=active_before, after=active_after))

    # --- success criteria: what the fix claims to improve ----------------------------------
    rules = entry.get("success") or {}
    wait_before, wait_after = _number(before_facts.get("search_failure_wait_seconds")), _number(after_facts.get("search_failure_wait_seconds"))
    if "search_failure_wait_seconds_max" in rules:
        limit = float(rules["search_failure_wait_seconds_max"])
        if after_facts.get("search_failures", 0) == 0 and after_samples:
            success.append(_check("failing searches end fast", MISSING,
                                  "no search failed in the after run, so the fault did not exercise the fix"))
        elif wait_after is None:
            success.append(_check("failing searches end fast", MISSING, "no probe samples for the after run"))
        else:
            better = wait_before is None or wait_after < wait_before
            status = PASS if wait_after <= limit and better else FAIL
            success.append(_check("failing searches end fast", status,
                                  f"longest failing search must be <= {limit} s and shorter than before",
                                  before_seconds=wait_before, after_seconds=wait_after, limit_seconds=limit))
    if "failure_status_codes" in rules:
        allowed = {str(code) for code in rules["failure_status_codes"]}
        errors = after_facts.get("search_failure_errors") or []
        if not errors:
            success.append(_check("failures are explicit server answers", MISSING, "no failed search in the after run"))
        else:
            ok = all(str(error) in allowed for error in errors)
            success.append(_check("failures are explicit server answers", PASS if ok else FAIL,
                                  f"every failed search must be HTTP {'/'.join(sorted(allowed))}, not a client timeout",
                                  before=before_facts.get("search_failure_errors"), after=errors))

    # --- guards: the fix must not make anything else worse ---------------------------------
    rails = entry.get("guards") or {}
    if "write_error_rate_increase_max" in rails:
        eb, ea = _worst(before, "write_error_rate"), _worst(after, "write_error_rate")
        limit = float(rails["write_error_rate_increase_max"])
        guards.append(_check("write errors not worse", MISSING if None in (eb, ea) else PASS if ea - eb <= limit else FAIL,
                             f"worst write error rate may rise by at most {limit:.0%}", before=eb, after=ea))
    if "write_p95_ratio_max" in rails:
        # Compared as a multiple of each run's own baseline, so two different baselines stay fair.
        # Measured from raw write samples strictly between injection and rollback. The kill
        # switch's 30 s windows are the fallback only: their first polls reach ~28 s back into the
        # pre-fault period, so one slow write before the fault can decide them.
        multiples, sources = [], []
        raw = [_fault_write_p95(run, rows) for run, rows in ((before, before_samples), (after, after_samples))]
        like_for_like = all(source.startswith("raw") for _, source in raw)
        for run, (p95, source) in zip((before, after), raw):
            if not like_for_like:  # never compare raw samples on one side with windows on the other
                p95 = _worst(run, "write_p95_ms")
                source = "worst kill-switch window (raw samples missing on one side)" if p95 is not None else "missing"
            base = _number(run.get("baseline_write_p95_ms"))
            multiples.append(round(p95 / base, 3) if p95 is not None and base else None)
            sources.append(source)
        limit = float(rails["write_p95_ratio_max"])
        mb, ma = multiples
        guards.append(_check("write latency not worse", MISSING if None in (mb, ma) else PASS if ma <= mb * limit else FAIL,
                             f"write p95 during the fault, as a multiple of baseline, may grow by at most {limit}x",
                             before_multiple=mb, after_multiple=ma, before_source=sources[0], after_source=sources[1],
                             worst_kill_switch_window_ms={"before": _worst(before, "write_p95_ms"), "after": _worst(after, "write_p95_ms")}))
    if rails.get("recovery_verified"):
        state = na["recovery_state"]
        guards.append(_check("after run recovered", PASS if state == "recovered" else MISSING if state == "unknown" else FAIL,
                             "recovery_verified must be true", recovery_seconds=na["recovery_time_seconds"]))
        if na.get("cleanup_state") not in (None, "complete"):
            guards.append(_check("after run cleaned up", FAIL, "rollback must have completed"))

    checks = conditions + success + guards
    if any(item["status"] == FAIL for item in checks):
        verdict = "not_verified"
    elif any(item["status"] == MISSING for item in checks) or not success:
        verdict = "inconclusive"
    else:
        verdict = "verified"
    return {
        "schema_version": "1.0",
        "remediation": name,
        "kind": entry.get("kind"),
        "addresses": entry.get("addresses"),
        "change": entry.get("change"),
        "before_run_id": nb["run_id"],
        "after_run_id": na["run_id"],
        "verdict": verdict,
        "conditions": conditions,
        "success_criteria": success,
        "guards": guards,
        "request_facts": {"before": before_facts, "after": after_facts},
        "samples_after_recovery_excluded": {"before": dropped_before, "after": dropped_after},
        "retest": retest,
    }


def render_markdown(result: dict[str, Any]) -> str:
    icon = {PASS: "PASS", FAIL: "FAIL", MISSING: "NOT MEASURED"}
    lines = [
        f"# Re-run after fix: `{result['remediation']}` ({result['kind']})",
        "",
        f"**Verdict: {result['verdict'].replace('_', ' ').upper()}**  ",
        f"Before `{result['before_run_id']}` -> after `{result['after_run_id']}`",
        "",
        f"- Weakness addressed: {result['addresses']}",
        f"- Change: {result['change']}",
        "",
    ]
    for title, key in (("Conditions (fair, real, like-for-like)", "conditions"),
                       ("Success criteria (what the fix claims)", "success_criteria"),
                       ("No-regression guards", "guards")):
        lines += [f"## {title}", "", "| Check | Result | Detail | Values |", "|---|---|---|---|"]
        for item in result[key]:
            values = {k: v for k, v in item.items() if k not in ("check", "status", "detail")}
            lines.append(f"| {item['check']} | {icon[item['status']]} | {item['detail']} | "
                         f"{', '.join(f'{k}={v}' for k, v in values.items()) or '-'} |")
        lines.append("")
    measurements = result["retest"]["measurements"]
    lines += ["## Before vs after (measured)", "", "| Measure | Before | After | Delta |", "|---|---|---|---|"]
    for key, item in measurements.items():
        if isinstance(item, dict) and "delta" in item:
            lines.append(f"| {key} | {item.get('before')} | {item.get('after')} | {item.get('delta')} |")
    dropped = result.get("samples_after_recovery_excluded") or {}
    lines += ["", f"Samples after recovery was verified are not counted (before: {dropped.get('before', 0)}, "
              f"after: {dropped.get('after', 0)}): the experiment is over at that point.",
              "Sources: the two run records and their probe samples (`python -m ai.request_evidence`).", ""]
    return "\n".join(lines)


def _load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _samples(run: dict[str, Any], explicit: str | None, metrics_dir: Path) -> list[dict[str, Any]]:
    if explicit:
        return load_samples(explicit)
    committed = EVIDENCE / "samples" / f"{run.get('run_id')}.jsonl"
    if committed.exists():
        return load_samples(committed)
    return extract_samples(run, metrics_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Judge a fix on a before/after pair of real runs (configs/remediations.yaml).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="registered fixes")
    check = sub.add_parser("verify", help="verify one fix")
    check.add_argument("remediation")
    check.add_argument("--before", help="run record JSON before the fix")
    check.add_argument("--after", help="run record JSON after the fix")
    check.add_argument("--evidence", action="store_true", help="use the before/after runs named in the registry (docs/evidence/windows-host)")
    check.add_argument("--before-samples", help="probe samples JSONL (default: committed samples, else cut from --metrics-dir)")
    check.add_argument("--after-samples")
    check.add_argument("--metrics-dir", type=Path, default=ROOT / "data" / "metrics")
    check.add_argument("--markdown", type=Path, help="also write the report as Markdown to this path")
    args = parser.parse_args(argv)
    registry = load_registry()
    if args.command == "list":
        for key, entry in registry.items():
            print(f"{key} [{entry.get('kind')}]: {entry.get('addresses')}")
        return 0
    if args.evidence:
        refs = (registry.get(args.remediation) or {}).get("evidence") or {}
        if not refs:
            parser.error("this fix has no committed evidence pair; pass --before and --after")
        args.before, args.after = (str(EVIDENCE / f"{refs[side]}.json") for side in ("before", "after"))
    if not args.before or not args.after:
        parser.error("--before and --after are required (or --evidence)")
    before, after = _load(args.before), _load(args.after)
    result = verify(args.remediation, before, after,
                    _samples(before, args.before_samples, args.metrics_dir),
                    _samples(after, args.after_samples, args.metrics_dir), registry)
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(result), encoding="utf-8", newline="\n")
        print(f"Saved {args.markdown}")
    print(render_markdown(result))
    return {"verified": 0, "inconclusive": 1, "not_verified": 3}[result["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
