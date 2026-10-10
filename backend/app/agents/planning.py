from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ai.run_record import normalize_run_record

from ..store import LocalRepository


DITTO_ROLES = {
    "gateway": "Gateway",
    "things-search": "Things Search",
    "things": "Things",
    "policies": "Policies",
    "connectivity": "Connectivity",
    "mongodb": "MongoDB",
    "nginx": "Nginx",
}

def load_planning_policy(path: Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream) or {}
    if not isinstance(raw, dict):
        raise ValueError("Experiment policy must be a mapping.")

    scope = raw.get("scope", {})
    if not isinstance(scope, dict):
        raise ValueError("Experiment policy scope must be a mapping.")
    targets = scope.get("allowed_targets", [])
    denied = set(scope.get("denied_targets", []))
    if not isinstance(targets, list) or any(not isinstance(target, str) for target in targets):
        raise ValueError("Policy allowed_targets must be a list of exact container names.")

    try:
        global_limit = int(raw.get("max_duration_seconds", 0))
        configured_actions = raw.get("actions", {})
        if not isinstance(configured_actions, dict):
            raise ValueError
        actions: dict[str, dict[str, int]] = {}
        for fault, config in configured_actions.items():
            if not isinstance(config, dict) or config.get("enabled") is not True:
                continue
            maximum = min(global_limit, int(config.get("max_duration_seconds", global_limit)))
            default = int(config.get("duration_seconds_default", maximum))
            if maximum > 0 and default > 0:
                actions[str(fault)] = {
                    "duration_seconds_default": min(default, maximum),
                    "max_duration_seconds": maximum,
                }
    except (TypeError, ValueError) as error:
        raise ValueError("Policy action duration limits must be positive integers.") from error

    normalized = dict(raw)
    normalized["scope"] = {**scope, "allowed_targets": [target for target in targets if target not in denied]}
    normalized["planning_actions"] = actions
    return normalized


def _graph_impact_context(graph: dict[str, Any]) -> tuple[dict[str, set[str]], dict[str, str]]:
    nodes = {node["id"] for node in graph.get("nodes", []) if isinstance(node, dict) and isinstance(node.get("id"), str)}
    dependents = {node: set() for node in nodes}
    paths_by_target = graph.get("impact_paths", {})
    summaries: dict[str, str] = {}

    for target in nodes:
        paths = paths_by_target.get(target, []) if isinstance(paths_by_target, dict) else []
        if not isinstance(paths, list):
            paths = []
        descriptions: list[str] = []
        sources: set[str] = set()
        for entry in paths:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if not isinstance(path, list) or len(path) < 2 or path[0] != target:
                continue
            impacted = entry.get("impacted_component")
            if isinstance(impacted, str):
                if len(path) == 2:
                    dependents[target].add(impacted)
                descriptions.append(" -> ".join(str(part) for part in path))
            for edge in entry.get("edges", []):
                if isinstance(edge, dict):
                    provenance = edge.get("provenance") or edge.get("provenance_type")
                    if isinstance(provenance, str) and provenance:
                        sources.add(provenance)

        if not paths:
            for edge in graph.get("edges", []):
                if not isinstance(edge, dict) or not (
                    edge.get("runtime_observed") is True or edge.get("evidence_type") == "runtime"
                ):
                    continue
                if edge.get("target") == target and edge.get("source") in nodes:
                    dependent = edge["source"]
                    dependents[target].add(dependent)
                    descriptions.append(f"{target} -> {dependent}")
                    provenance = edge.get("provenance") or edge.get("provenance_type") or "runtime-observed graph edge"
                    sources.add(str(provenance))

        if dependents[target]:
            dependent_text = f"{len(dependents[target])} direct graph dependent(s): {', '.join(sorted(dependents[target]))}"
        else:
            dependent_text = "no direct dependents recorded in ai/dependency_map.json"
        path_text = f"; impact paths: {'; '.join(dict.fromkeys(descriptions[:4]))}" if descriptions else ""
        graph_source = f"ai/dependency_map.json ({', '.join(sorted(sources))})" if sources else "ai/dependency_map.json"
        source_text = f"; source: {graph_source}"
        summaries[target] = dependent_text + path_text + source_text

    return dependents, summaries


class ExperimentPlanningAgent:
    name = "AI Experiment Planning Agent"

    @staticmethod
    def should_count_history_record(record: Any) -> bool:
        if record is None:
            return False
        normalized = normalize_run_record(record) if isinstance(record, dict) else record
        predicted = normalized.get("prediction", {}).get("predicted_slis", {}) if isinstance(normalized, dict) else {}
        terminal_status = normalized.get("status") if isinstance(normalized, dict) else None
        adverse_terminal = (
            terminal_status == "aborted" or normalized.get("recovery_state") == "not_recovered"
            if isinstance(normalized, dict) else False
        )
        return (
            isinstance(normalized, dict)
            and not normalized["forced_test"]
            and not normalized["dry_run"]
            and normalized.get("source") == "runner_record"
            and (
                adverse_terminal
                or (
                    terminal_status == "completed"
                    and normalized.get("cleanup_state") == "complete"
                    and normalized.get("recovery_state") == "recovered"
                    and bool(predicted)
                    and set(predicted).issubset(normalized.get("sli_windows", {}))
                )
            )
        )

    def __init__(self, policy_path: Path, repository: LocalRepository, planner_config_path: Path | None = None) -> None:
        self.policy = load_planning_policy(policy_path)
        self.repository = repository
        self.planner_config = self._load_planner_config(planner_config_path or Path(policy_path).parent / "planner.yaml")

    def plan(self, graph: dict[str, Any], telemetry: dict[str, Any], baseline: dict[str, Any], max_candidates: int = 3) -> dict[str, Any]:
        if graph.get("status") != "available":
            raise ValueError("Architecture discovery is unavailable; no candidate can be justified.")
        if telemetry.get("status") != "available" or telemetry.get("coverage", 0) < 0.8:
            raise ValueError("Fresh, healthy telemetry with at least 80% coverage is required.")
        if baseline.get("telemetry_status") != "available" or baseline.get("graph_version") != graph.get("graph_version"):
            raise ValueError("The baseline must be healthy and match the current architecture snapshot.")
        if not self._baseline_is_fresh(baseline):
            raise ValueError("The baseline has expired; capture a new one before planning.")

        scope = self.policy.get("scope", {})
        allowed_targets = list(scope.get("allowed_targets", []))
        denied_targets = set(scope.get("denied_targets", []))
        risk_scores = self.policy.get("target_risk_scores", {})
        risk_cap = float(self.policy["max_risk_score"])
        max_duration = int(self.policy["max_duration_seconds"])
        actions = {name: config for name, config in (self.policy.get("actions") or {}).items() if isinstance(config, dict) and config.get("enabled")}
        duration_limits = self.policy["planning_actions"]
        nodes = {node["id"]: node for node in graph.get("nodes", [])}
        dependents_by_target, impact_summaries = _graph_impact_context(graph)
        max_dependents = max((len(items) for items in dependents_by_target.values()), default=0)
        candidates = []
        rejections = []

        measured = list(self.planner_config["measured_slis"])
        entries = []

        # Per-action targets can narrow or extend the scope allowlist, but never override an explicit deny.
        pairs: list[tuple[str, str]] = []
        for action, config in actions.items():
            explicit = config.get("targets")
            for target in (explicit if isinstance(explicit, list) else allowed_targets):
                if target in denied_targets:
                    continue
                pairs.append((action, str(target)))

        for action, policy_target in pairs:
            config = actions[action]
            node = nodes.get(policy_target)
            if node is None:
                expected_role = DITTO_ROLES.get(policy_target)
                node = next((item for item in nodes.values() if expected_role and item.get("labels", {}).get("ditto_role") == expected_role), None)
            if node is None:
                rejections.append({"target": policy_target, "action": action, "reason": "target is not in the discovered deployment"})
                continue
            target = node["id"]
            action_limits = duration_limits[action]
            duration = min(action_limits["duration_seconds_default"], action_limits["max_duration_seconds"], max_duration)
            risk = float(risk_scores.get(policy_target, 1.0))
            if risk > risk_cap or duration < 1:
                rejections.append({"target": target, "action": action, "reason": "risk or duration exceeds deterministic policy"})
                continue
            effects = self._effects_for(action, target, policy_target)
            predicted_slis = {sli: effects.get(sli, "hold") for sli in measured}
            if not any(value != "hold" for value in predicted_slis.values()):
                rejections.append({"target": target, "action": action, "reason": "no predicted effect is observable with the measured SLIs"})
                continue
            dependents = dependents_by_target.get(target, set())
            role = node.get("labels", {}).get("ditto_role", DITTO_ROLES.get(target, target))
            structural = len(dependents) / max_dependents if max_dependents else 0.0
            impact = round(structural * float(config.get("impact_multiplier", 1.0)), 4)
            history = self.repository.history_for(target, action)
            novelty = 1.0 / (1.0 + history["trials"])
            uncertainty = 1.0 / ((1.0 + history["trials"]) ** 0.5)
            selection_score = 0.40 * impact + 0.35 * novelty + 0.25 * uncertainty
            candidate_id = hashlib.sha256(f"{graph['graph_version']}:{baseline['baseline_id']}:{target}:{action}".encode("utf-8")).hexdigest()[:16]
            predicted_impacts = ["external_http_websocket_api"] if role == "Gateway" else [target]
            metric_refs = sorted({f"metric:{item['metric']}" for item in telemetry.get("observations", []) if item.get("quality") == "fresh"})
            candidate = {
                "schema_version": "1.0",
                "id": f"{action}-{target}-{candidate_id}",
                "target_component": target,
                "action": action,
                "duration_seconds": duration,
                "hypothesis": self._hypothesis(role, action, target, config),
                "predicted_impacts": predicted_impacts,
                "predicted_slis": predicted_slis,
                "information_gain": novelty,
                "risk_score": risk,
                "evidence_refs": [f"graph:{graph['graph_version']}", f"baseline:{baseline['baseline_id']}", *metric_refs],
                "approval_required": True,
                "selection_score": selection_score,
                "score_terms": {"impact": impact, "novelty": novelty, "uncertainty": uncertainty},
                "ranking_method": "weighted_heuristic_v1",
            }
            entries.append({
                "candidate": candidate,
                "impact": impact,
                "trials": history["trials"],
                "role": role,
                "dependents": len(dependents),
                "impact_summary": impact_summaries.get(target, "no dependents recorded in ai/dependency_map.json"),
            })

        first_run = bool(entries) and all(entry["trials"] == 0 for entry in entries)
        fallback = False
        if first_run:
            limit = float(self.planner_config["first_run_max_impact"])
            eligible = [entry for entry in entries if entry["impact"] <= limit]
            if not eligible:
                lowest = min(entry["impact"] for entry in entries)
                eligible = [entry for entry in entries if entry["impact"] == lowest]
                fallback = True
            for entry in entries:
                if entry not in eligible:
                    rejections.append({
                        "target": entry["candidate"]["target_component"],
                        "action": entry["candidate"]["action"],
                        "reason": f"deferred: the first experiment must have impact <= {limit:.2f} (this one is {entry['impact']:.2f}); eligible after one completed run",
                    })
            entries = eligible
        for entry in entries:
            entry["candidate"]["reason"] = self._reason(entry, first_run, fallback)
            candidates.append(entry["candidate"])

        candidates.sort(key=lambda item: (-item["selection_score"], item["risk_score"], item["target_component"]))
        return {
            "schema_version": "1.0",
            "plan_id": f"plan-{hashlib.sha256((graph['graph_version'] + baseline['baseline_id']).encode('utf-8')).hexdigest()[:12]}",
            "ranking_method": "weighted_heuristic_v1",
            "candidates": candidates[:max(1, min(max_candidates, 10))],
            "policy_rejections": rejections,
            "execution_enabled": False,
        }

    @staticmethod
    def _load_planner_config(path: Path) -> dict[str, Any]:
        config: dict[str, Any] = {"measured_slis": ["write", "search", "freshness"], "first_run_max_impact": 0.35, "predicted_effects": {}}
        if Path(path).is_file():
            with Path(path).open("r", encoding="utf-8") as stream:
                loaded = yaml.safe_load(stream) or {}
            for key in config:
                if key in loaded:
                    config[key] = loaded[key]
        return config

    @staticmethod
    def _reason(entry: dict[str, Any], first_run: bool, fallback: bool) -> str:
        candidate = entry["candidate"]
        hit = [sli for sli, value in candidate["predicted_slis"].items() if value != "hold"]
        text = (f"{candidate['target_component']} ({entry['role']}): {entry['impact_summary']}; "
            f"predicted to affect {', '.join(hit)}; ")
        text += "never tested before" if entry["trials"] == 0 else f"tested {entry['trials']} time(s) before"
        if first_run:
            text += ". Chosen as a low-blast-radius first experiment" + (" (lowest impact available)" if fallback else "")
        return text + "."

    def _baseline_is_fresh(self, baseline: dict[str, Any]) -> bool:
        try:
            captured = datetime.fromisoformat(baseline["captured_at"].replace("Z", "+00:00"))
            max_age = int(self.policy["baseline_max_age_seconds"])
        except (KeyError, ValueError, TypeError):
            return False
        age = (datetime.now(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds()
        return 0 <= age <= max_age

    def _effects_for(self, action: str, target: str, policy_target: str) -> dict[str, str]:
        """Read action-specific SLI effects, falling back to a target-wide policy effect."""
        effects = self.planner_config["predicted_effects"]
        by_action = effects.get(action)
        if isinstance(by_action, dict) and isinstance(by_action.get(target) or by_action.get(policy_target), dict):
            return by_action.get(target) or by_action.get(policy_target)
        legacy = effects.get(target) or effects.get(policy_target)
        if isinstance(legacy, dict) and all(isinstance(v, str) for v in legacy.values()):
            return legacy
        return {}

    @staticmethod
    def _hypothesis(role: str, action: str, target: str, config: dict[str, Any]) -> str:
        configured = config.get("hypothesis")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        action_label = action.replace("_", " ")
        return (
            f"A bounded {action_label} targeting {target} ({role}) may affect its documented responsibility; "
            "compare measured SLIs through recovery."
        )
