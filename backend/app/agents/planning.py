from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .discovery import DITTO_ROLES
from ..store import LocalRepository


class ExperimentPlanningAgent:
    name = "AI Experiment Planning Agent"

    def __init__(self, policy_path: Path, repository: LocalRepository) -> None:
        with Path(policy_path).open("r", encoding="utf-8") as stream:
            self.policy = yaml.safe_load(stream) or {}
        self.repository = repository

    def plan(self, graph: dict[str, Any], telemetry: dict[str, Any], baseline: dict[str, Any], max_candidates: int = 3) -> dict[str, Any]:
        if graph.get("status") != "available":
            raise ValueError("Architecture discovery is unavailable; no candidate can be justified.")
        if telemetry.get("status") != "available" or telemetry.get("coverage", 0) < 0.8:
            raise ValueError("Fresh, healthy telemetry with at least 80% coverage is required.")
        if baseline.get("telemetry_status") != "available" or baseline.get("graph_version") != graph.get("graph_version"):
            raise ValueError("The baseline must be healthy and match the current architecture snapshot.")
        if not self._baseline_is_fresh(baseline):
            raise ValueError("The baseline has expired; capture a new one before planning.")

        allowed_targets = self.policy.get("scope", {}).get("allowed_targets", [])
        denied_targets = set(self.policy.get("scope", {}).get("denied_targets", []))
        risk_scores = self.policy.get("target_risk_scores", {})
        risk_cap = float(self.policy.get("max_risk_score", 0))
        max_duration = int(self.policy.get("max_duration_seconds", 0))
        duration = min(int(self.policy.get("actions", {}).get("restart_container", {}).get("duration_seconds_default", 10)), max_duration)
        nodes = {node["id"]: node for node in graph.get("nodes", [])}
        candidate_targets = []
        for policy_target in allowed_targets:
            if policy_target in denied_targets:
                continue
            expected_role = DITTO_ROLES.get(policy_target)
            for component_id, node in nodes.items():
                if component_id == policy_target or (expected_role and node.get("labels", {}).get("ditto_role") == expected_role):
                    candidate_targets.append((policy_target, component_id, node))
        max_dependents = max((value.get("configured_dependents", 0) for value in graph.get("criticality", {}).values()), default=0)
        candidates = []
        rejections = []

        for policy_target, target, node in candidate_targets:
            if node.get("kind") != "ditto_service":
                rejections.append({"target": target, "reason": "target is not a discovered Ditto service"})
                continue
            risk = float(risk_scores.get(policy_target, 1.0))
            if risk > risk_cap or duration < 1:
                rejections.append({"target": target, "reason": "risk or duration exceeds deterministic policy"})
                continue
            dependents = int(graph.get("criticality", {}).get(target, {}).get("configured_dependents", 0))
            role = node.get("labels", {}).get("ditto_role", DITTO_ROLES.get(target, target))
            impact = 1.0 if role == "Gateway" else (dependents / max_dependents if max_dependents else 0.25)
            history = self.repository.history_for(target, "restart_container")
            novelty = 1.0 / (1.0 + history["trials"])
            uncertainty = 1.0 / ((1.0 + history["trials"]) ** 0.5)
            selection_score = 0.40 * impact + 0.35 * novelty + 0.25 * uncertainty
            candidate_id = hashlib.sha256(f"{graph['graph_version']}:{baseline['baseline_id']}:{target}:restart_container".encode("utf-8")).hexdigest()[:16]
            predicted_impacts = ["external_http_websocket_api"] if role == "Gateway" else [target]
            metric_refs = sorted({f"metric:{item['metric']}" for item in telemetry.get("observations", []) if item.get("quality") == "fresh"})
            candidates.append({
                "schema_version": "1.0",
                "id": f"restart-{target}-{candidate_id}",
                "target_component": target,
                "action": "restart_container",
                "duration_seconds": duration,
                "hypothesis": self._hypothesis(role),
                "predicted_impacts": predicted_impacts,
                "information_gain": novelty,
                "risk_score": risk,
                "evidence_refs": [f"graph:{graph['graph_version']}", f"baseline:{baseline['baseline_id']}", *metric_refs],
                "approval_required": True,
                "selection_score": selection_score,
                "score_terms": {"impact": impact, "novelty": novelty, "uncertainty": uncertainty},
                "ranking_method": "weighted_heuristic_v1",
            })

        candidates.sort(key=lambda item: (-item["selection_score"], item["risk_score"], item["target_component"]))
        return {
            "schema_version": "1.0",
            "plan_id": f"plan-{hashlib.sha256((graph['graph_version'] + baseline['baseline_id']).encode('utf-8')).hexdigest()[:12]}",
            "ranking_method": "weighted_heuristic_v1",
            "candidates": candidates[:max(1, min(max_candidates, 10))],
            "policy_rejections": rejections,
            "execution_enabled": False,
        }

    def _baseline_is_fresh(self, baseline: dict[str, Any]) -> bool:
        try:
            captured = datetime.fromisoformat(baseline["captured_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            return False
        age = (datetime.now(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds()
        return 0 <= age <= int(self.policy.get("baseline_max_age_seconds", 3600))

    @staticmethod
    def _hypothesis(role: str) -> str:
        if role == "Gateway":
            return "A short Gateway restart interrupts the external API; health and a functional canary should recover after the container returns."
        return f"A short {role} restart may affect its documented responsibility; compare service health and relevant API behavior through recovery."