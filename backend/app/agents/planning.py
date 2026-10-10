from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .discovery import DITTO_ROLES
from ..store import LocalRepository


# How disruptive each fault is relative to a full stop of the same target (impact term).
ACTION_SEVERITY = {
    "restart_container": 1.0,
    "stop_container": 1.0,
    "pause_container": 1.0,
    "cpu_throttle": 0.6,
    "traffic_burst": 0.4,
}
ACTION_PREFIX = {
    "restart_container": "restart",
    "stop_container": "stop",
    "pause_container": "pause",
    "cpu_throttle": "throttle",
    "traffic_burst": "burst",
}


class ExperimentPlanningAgent:
    name = "AI Experiment Planning Agent"

    def __init__(self, policy_path: Path, repository: LocalRepository, planner_config_path: Path | None = None) -> None:
        with Path(policy_path).open("r", encoding="utf-8") as stream:
            self.policy = yaml.safe_load(stream) or {}
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
        risk_cap = float(self.policy.get("max_risk_score", 0))
        max_duration = int(self.policy.get("max_duration_seconds", 0))
        actions = {name: config for name, config in (self.policy.get("actions") or {}).items() if isinstance(config, dict) and config.get("enabled")}
        nodes = {node["id"]: node for node in graph.get("nodes", [])}
        max_dependents = max((value.get("configured_dependents", 0) for value in graph.get("criticality", {}).values()), default=0)
        candidates = []
        rejections = []

        measured = list(self.planner_config["measured_slis"])
        entries = []

        # Every enabled action x every target the policy allows for it. A denied target is
        # only considered where an action lists it explicitly (e.g. mongodb for cpu_throttle);
        # the safety controller re-checks all of this at execution time.
        pairs: list[tuple[str, str]] = []
        for action, config in actions.items():
            explicit = config.get("targets")
            for target in (explicit if isinstance(explicit, list) else allowed_targets):
                if target in denied_targets and not (isinstance(explicit, list) and target in explicit):
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
            duration = min(int(config.get("duration_seconds_default", 10)), max_duration)
            risk = float(risk_scores.get(policy_target, 1.0))
            if risk > risk_cap or duration < 1:
                rejections.append({"target": target, "action": action, "reason": "risk or duration exceeds deterministic policy"})
                continue
            effects = self._effects_for(action, target, policy_target)
            predicted_slis = {sli: effects.get(sli, "hold") for sli in measured}
            if not any(value != "hold" for value in predicted_slis.values()):
                rejections.append({"target": target, "action": action, "reason": "no predicted effect is observable with the measured SLIs"})
                continue
            dependents = int(graph.get("criticality", {}).get(target, {}).get("configured_dependents", 0))
            role = node.get("labels", {}).get("ditto_role", DITTO_ROLES.get(target, target))
            # The gateway fronts every request and the database backs every service, so both
            # carry full structural impact whatever the configured-dependents count says.
            if role == "Gateway" or node.get("kind") == "database":
                structural = 1.0
            else:
                structural = dependents / max_dependents if max_dependents else 0.25
            impact = round(structural * ACTION_SEVERITY.get(action, 1.0), 4)
            history = self.repository.history_for(target, action)
            novelty = 1.0 / (1.0 + history["trials"])
            uncertainty = 1.0 / ((1.0 + history["trials"]) ** 0.5)
            selection_score = 0.40 * impact + 0.35 * novelty + 0.25 * uncertainty
            candidate_id = hashlib.sha256(f"{graph['graph_version']}:{baseline['baseline_id']}:{target}:{action}".encode("utf-8")).hexdigest()[:16]
            predicted_impacts = ["external_http_websocket_api"] if role == "Gateway" else [target]
            metric_refs = sorted({f"metric:{item['metric']}" for item in telemetry.get("observations", []) if item.get("quality") == "fresh"})
            candidate = {
                "schema_version": "1.0",
                "id": f"{ACTION_PREFIX.get(action, action)}-{target}-{candidate_id}",
                "target_component": target,
                "action": action,
                "duration_seconds": duration,
                "hypothesis": self._hypothesis(role, action, target),
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
            entries.append({"candidate": candidate, "impact": impact, "trials": history["trials"], "role": role, "dependents": dependents})

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
        text = (f"{candidate['target_component']} ({entry['role']}): {entry['dependents']} configured dependent(s); "
                f"predicted to affect {', '.join(hit)}; ")
        text += "never tested before" if entry["trials"] == 0 else f"tested {entry['trials']} time(s) before"
        if first_run:
            text += ". Chosen as a low-blast-radius first experiment" + (" (lowest impact available)" if fallback else "")
        return text + "."

    def _baseline_is_fresh(self, baseline: dict[str, Any]) -> bool:
        try:
            captured = datetime.fromisoformat(baseline["captured_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            return False
        age = (datetime.now(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds()
        return 0 <= age <= int(self.policy.get("baseline_max_age_seconds", 3600))

    def _effects_for(self, action: str, target: str, policy_target: str) -> dict[str, str]:
        """Predicted SLI effects. Nested by action; a flat per-target entry means restart_container."""
        effects = self.planner_config["predicted_effects"]
        by_action = effects.get(action)
        if isinstance(by_action, dict) and isinstance(by_action.get(target) or by_action.get(policy_target), dict):
            return by_action.get(target) or by_action.get(policy_target)
        legacy = effects.get(target) or effects.get(policy_target)
        if action == "restart_container" and isinstance(legacy, dict) and all(isinstance(v, str) for v in legacy.values()):
            return legacy
        return {}

    @staticmethod
    def _hypothesis(role: str, action: str = "restart_container", target: str = "") -> str:
        if action == "pause_container":
            return f"Freezing {role} (pause) should fail its requests while frozen; it should resume without data loss after unpause."
        if action == "cpu_throttle":
            return f"Limiting {target} to a fraction of a CPU should raise latency on the paths that depend on it without failing requests; latency should return to baseline after the limit is removed."
        if action == "traffic_burst":
            return "A short burst of extra fleet traffic through the API should raise latency but stay within the abort thresholds; latency should return to baseline after the burst."
        if action == "stop_container":
            return f"Stopping {role} should fail its requests for the whole window; health and a functional canary should recover after it starts again."
        if role == "Gateway":
            return "A short Gateway restart interrupts the external API; health and a functional canary should recover after the container returns."
        return f"A short {role} restart may affect its documented responsibility; compare service health and relevant API behavior through recovery."
