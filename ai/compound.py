"""Combined failures: compose a staged two-fault candidate and judge the interaction afterwards.

Nothing here executes anything. `compose_compound` turns two already-planned, single-fault
candidates into one immutable compound candidate (the control plane re-validates it against
the policy's `compound` rules); `interaction_effect` compares a compound run with solo runs of
the same faults and says whether combining them made things worse than either alone.

    python -m ai.compound --help
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .ranking import _target_and_action
from .run_record import normalize_run_record

ORDER = {"hold": 0, "degrade": 1, "fail": 2}
# A compound counts as "amplified" only if it exceeds the worst solo run by this factor AND
# by an absolute margin, so measurement noise on a near-zero metric is never reported.
AMPLIFY_FACTOR = 1.25
DAMPEN_FACTOR = 0.75
# 0.10: a 30 s window holds ~60 writes, so a few failed requests (one blip) must not count.
ABS_MARGIN = {"error_rate": 0.10, "p95_ms": 100.0, "p95_s": 2.0}
METRICS = ("error_rate", "p95_ms", "p95_s")
SLIS = ("write", "search", "freshness")
_ENRICHMENT_KEYS = frozenset({"plan_id", "candidate_hash", "approval_status", "approval"})


def _fault_key(fault: dict[str, Any]) -> str:
    return f"{fault['action']}:{fault['target_component']}"


def merge_predictions(*predictions: dict[str, str]) -> dict[str, str]:
    """Worst-of per SLI: a combined failure is predicted at least as bad as its worst member."""
    merged: dict[str, str] = {}
    for prediction in predictions:
        for sli, level in (prediction or {}).items():
            if ORDER.get(level, 0) >= ORDER.get(merged.get(sli, "hold"), 0):
                merged[sli] = level
    return merged


def predicted_interaction(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """Why the pair is worth running: SLIs both faults are predicted to hurt can compound."""
    a, b = first.get("predicted_slis") or {}, second.get("predicted_slis") or {}
    shared = sorted(sli for sli in SLIS if a.get(sli, "hold") != "hold" and b.get(sli, "hold") != "hold")
    return {
        "shared_slis": shared,
        "expectation": "amplified" if shared else "independent",
        "rationale": (
            f"both faults are predicted to hurt {', '.join(shared)}; load on the entry point while a "
            "downstream service is degraded can multiply client retries (metastable-failure pattern)"
            if shared else "the faults are predicted to hit different SLIs, so effects should add, not multiply"
        ),
    }


def compose_compound(first: dict[str, Any], second: dict[str, Any], stagger_seconds: int) -> dict[str, Any]:
    """Build the immutable compound candidate. `first` starts at t=0, `second` at t=stagger."""
    for item in (first, second):
        if item.get("kind") == "compound":
            raise ValueError("Only single-fault candidates can be combined.")
    clean = [{k: v for k, v in item.items() if k not in _ENRICHMENT_KEYS} for item in (first, second)]
    faults = []
    for item, offset in zip(clean, (0, int(stagger_seconds))):
        faults.append({
            "action": item["action"],
            "target_component": item["target_component"],
            "duration_seconds": int(item["duration_seconds"]),
            "start_offset_seconds": offset,
            "risk_score": float(item["risk_score"]),
        })
    survive = 1.0
    for fault in faults:
        survive *= 1.0 - fault["risk_score"]
    interaction = predicted_interaction(*clean)
    names = [_fault_key(fault) for fault in faults]
    digest = hashlib.sha256(json.dumps([names, [f["start_offset_seconds"] for f in faults], faults[0]["duration_seconds"],
                                         faults[1]["duration_seconds"]]).encode("utf-8")).hexdigest()[:16]
    baselines = {ref for item in clean for ref in item.get("evidence_refs", []) if str(ref).startswith("baseline:")}
    if len(baselines) != 1:
        raise ValueError("Both candidates must come from the same baseline.")
    return {
        "schema_version": "1.0",
        "id": f"compound-{digest}",
        "kind": "compound",
        "target_component": "+".join(f["target_component"] for f in faults),
        "action": "compound:" + "+".join(f["action"] for f in faults),
        "faults": faults,
        "duration_seconds": max(f["start_offset_seconds"] + f["duration_seconds"] for f in faults),
        "hypothesis": (
            f"Running {names[1]} {int(stagger_seconds)} s after {names[0]} starts degrades the system "
            f"more than either fault alone. {interaction['rationale']}."
        ),
        "predicted_impacts": sorted({impact for item in clean for impact in item.get("predicted_impacts", [])}),
        "predicted_slis": merge_predictions(*(item.get("predicted_slis") or {} for item in clean)),
        "predicted_interaction": interaction,
        "information_gain": max(float(item.get("information_gain", 0.0)) for item in clean),
        "risk_score": round(1.0 - survive, 4),
        "evidence_refs": sorted({ref for item in clean for ref in item.get("evidence_refs", [])}
                                | {f"compound-of:{item['id']}" for item in clean}),
        "approval_required": True,
        "reason": f"Combined failure: {interaction['rationale']}.",
        "ranking_method": "compound_interaction_v1",
    }


def suggest_pairs(candidates: list[dict[str, Any]], allowed_pairs: Any, stagger_seconds: int = 10) -> list[dict[str, Any]]:
    """Policy-allowed pairs among planned candidates, amplifying pairs first (then lowest combined risk)."""
    by_key = {_fault_key(item): item for item in candidates if item.get("kind") != "compound"}
    suggestions = []
    for pair in allowed_pairs:
        keys = sorted(pair)
        if not all(key in by_key for key in keys):
            continue
        # The entry-point load starts first so the downstream fault lands on a loaded system.
        keys.sort(key=lambda key: (0 if key.startswith("traffic_burst:") else 1, key))
        first, second = by_key[keys[0]], by_key[keys[1]]
        try:
            candidate = compose_compound(first, second, stagger_seconds)
        except ValueError:
            continue
        suggestions.append({
            "first": first["id"], "second": second["id"], "stagger_seconds": stagger_seconds,
            "expectation": candidate["predicted_interaction"]["expectation"],
            "risk_score": candidate["risk_score"], "reason": candidate["reason"],
        })
    suggestions.sort(key=lambda item: (item["expectation"] != "amplified", item["risk_score"], item["first"]))
    return suggestions


def _worst(windows: dict[str, dict[str, Any]], sli: str, metric: str) -> float | None:
    value = (windows.get(sli) or {}).get(metric)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def interaction_effect(compound_run: dict[str, Any], solo_runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare the compound run with the worst solo run per SLI metric.

    `amplified`   : compound is >25% AND by an absolute margin worse than every solo run
    `dampened`    : compound is >25% better than the worst solo run (one fault masked the other)
    `no_interaction`: within the band
    Returns `inconclusive` unless the compound run is real evidence and every fault has a solo run.
    """
    compound = normalize_run_record(compound_run)
    result: dict[str, Any] = {"schema_version": "1.0", "run_id": compound["run_id"], "per_sli": {}, "solo_run_ids": []}
    if compound["dry_run"] or compound["forced_test"]:
        return {**result, "verdict": "inconclusive", "reason": "dry runs and forced tests are not interaction evidence"}
    wanted = {_fault_key({"action": f["action"], "target_component": f["target"]}) for f in compound_run.get("faults", [])}
    solo_windows: list[dict[str, dict[str, Any]]] = []
    covered: set[str] = set()
    for solo in solo_runs:
        normalized = normalize_run_record(solo)
        if normalized["dry_run"] or normalized["forced_test"] or solo.get("kind") == "compound":
            continue
        # Real runner records carry action/target in the diagnosis or experiment id, not top-level
        # fields; ai.ranking decodes every record format the repo has produced.
        target, action = _target_and_action(solo)
        key = f"{normalized['fault_type'] or action}:{normalized['target'] or target}"
        if key in wanted and normalized["status"] in ("completed", "aborted"):
            covered.add(key)
            solo_windows.append(normalized["sli_windows"])
            result["solo_run_ids"].append(normalized["run_id"])
    missing = sorted(wanted - covered)
    if missing:
        return {**result, "verdict": "inconclusive", "reason": f"no solo run to compare for: {', '.join(missing)}"}
    verdicts: list[str] = []
    for sli in SLIS:
        for metric in METRICS:
            observed = _worst(compound["sli_windows"], sli, metric)
            solos = [v for v in (_worst(w, sli, metric) for w in solo_windows) if v is not None]
            if observed is None or not solos:
                continue
            worst_solo = max(solos)
            margin = ABS_MARGIN[metric]
            if observed > worst_solo * AMPLIFY_FACTOR and observed - worst_solo >= margin:
                verdict = "amplified"
            elif observed < worst_solo * DAMPEN_FACTOR and worst_solo - observed >= margin:
                verdict = "dampened"
            else:
                verdict = "no_interaction"
            verdicts.append(verdict)
            result["per_sli"].setdefault(sli, {})[metric] = {
                "compound": observed, "worst_solo": worst_solo, "verdict": verdict,
            }
    if not verdicts:
        return {**result, "verdict": "inconclusive", "reason": "no comparable SLI measurements"}
    overall = "amplified" if "amplified" in verdicts else ("dampened" if "dampened" in verdicts else "no_interaction")
    return {**result, "verdict": overall,
            "reason": f"{verdicts.count(overall)} of {len(verdicts)} compared SLI metrics were {overall}"}


def interaction_finding(run: dict[str, Any], effect: dict[str, Any]) -> dict[str, Any] | None:
    """A finding only when the combination exposed something the solo runs did not."""
    if effect.get("verdict") != "amplified":
        return None
    amplified = [f"{sli}.{metric}" for sli, metrics in effect["per_sli"].items()
                 for metric, item in metrics.items() if item["verdict"] == "amplified"]
    return {
        "schema_version": "1.0",
        "id": f"{run['run_id']}-interaction",
        "run_id": run["run_id"],
        "severity": "high",
        "summary": "Combining the faults was worse than either alone (amplification): " + ", ".join(amplified) + ".",
        "evidence": [f"run:{run['run_id']}", *[f"solo:{item}" for item in effect["solo_run_ids"]], f"interaction:{effect['reason']}"],
        "recommendation": ("A single fault is absorbed but the pair is not: add a server-side timeout, bounded retries "
                           "with jitter and load shedding on the affected path, then re-run this combination."),
        "confidence": 0.8,
        "ai_generated": False,
    }


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Judge a combined-failure run against solo runs of the same faults.")
    parser.add_argument("compound_run", help="run record JSON of the combined failure")
    parser.add_argument("solo_runs", nargs="+", help="run record JSONs of the single-fault runs")
    args = parser.parse_args()
    load = lambda path: json.load(open(path, encoding="utf-8"))  # noqa: E731
    print(json.dumps(interaction_effect(load(args.compound_run), [load(p) for p in args.solo_runs]), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
