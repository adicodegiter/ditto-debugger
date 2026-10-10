from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .discovery import DITTO_ROLES, DiscoveryReport


DITTO_CLUSTER_MEMBERS = set(DITTO_ROLES)
DITTO_SLI_TAGS = {
    "Gateway": ["service_availability", "error_rate", "p95_latency"],
    "Policies": ["service_availability", "error_rate"],
    "Things": ["service_availability", "error_rate", "staleness_p95"],
    "Things-Search": ["service_availability", "error_rate", "p95_latency", "staleness_p95"],
    "Connectivity": ["service_availability", "error_rate", "p95_latency"],
}


@dataclass(frozen=True)
class ArchitectureMapperAgent:
    name: str = "Architecture and Dependency Mapping Agent"

    def map(self, report: DiscoveryReport) -> dict[str, Any]:
        nodes = [component.to_dict() for component in report.components]
        relationships = [relationship.to_dict() for relationship in report.relationships]
        component_ids = {node["id"] for node in nodes}
        relationships.extend(self._documented_ditto_edges(nodes))
        for node in nodes:
            role = node.get("labels", {}).get("ditto_role")
            node["affected_slis"] = DITTO_SLI_TAGS.get(role, ["service_availability"])
        provenance_types = {
            "static": "configured",
            "documented": "documented",
            "runtime": "runtime-observed",
            "operator": "manual",
        }
        for edge in relationships:
            edge["provenance_type"] = provenance_types.get(edge["evidence_type"], "manual")
        relationships.sort(key=lambda edge: (edge["source"], edge["target"], edge["relation"], edge["provenance"]))

        ditto_components = [
            node for node in nodes
            if node["id"] in DITTO_CLUSTER_MEMBERS or node.get("labels", {}).get("ditto_role") in DITTO_ROLES.values()
        ]
        ditto_members = sorted(node["id"] for node in ditto_components)
        discovered_roles = {
            node.get("labels", {}).get("ditto_role", DITTO_ROLES.get(node["id"]))
            for node in ditto_components
        }
        clusters = []
        if ditto_members:
            clusters.append({
                "id": "ditto-pekko-cluster",
                "members": ditto_members,
                "source": "ditto_docs:architecture-overview",
                "confidence": 1.0,
                "complete": discovered_roles == set(DITTO_ROLES.values()),
                "relationship": "documented_cluster_membership_not_request_flow",
            })

        dependent_counts = {component_id: 0 for component_id in component_ids}
        for relationship in relationships:
            if relationship["relation"] in {"compose_depends_on", "configured_database_host", "kubernetes_service_selector"}:
                dependent_counts[relationship["target"]] = dependent_counts.get(relationship["target"], 0) + 1
        maximum_dependents = max(dependent_counts.values(), default=0)
        criticality = {
            component_id: {
                "configured_dependents": count,
                "relative_score": count / maximum_dependents if maximum_dependents else 0.0,
                "evidence_type": "static",
            }
            for component_id, count in sorted(dependent_counts.items())
        }
        canonical_graph = {
            "nodes": nodes,
            "edges": relationships,
            "clusters": clusters,
            "network_zones": [zone.to_dict() for zone in report.network_zones],
            "criticality": criticality,
            "impact_paths": self._impact_paths(component_ids, relationships),
        }
        graph_version = hashlib.sha256(json.dumps(canonical_graph, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
        return {
            "schema_version": "1.0",
            "status": report.status,
            "graph_version": graph_version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            **canonical_graph,
            "source_files": list(report.source_files),
            "warnings": list(report.warnings),
        }

    @staticmethod
    def _documented_ditto_edges(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        role_nodes = {
            node.get("labels", {}).get("ditto_role"): node["id"]
            for node in nodes
            if node.get("labels", {}).get("ditto_role") in DITTO_ROLES.values()
        }
        evidence = "ditto_docs:architecture-overview"
        edges = []
        gateway = role_nodes.get("Gateway")
        if gateway:
            for role in ("Policies", "Things", "Things-Search", "Connectivity"):
                target = role_nodes.get(role)
                if target:
                    edges.append({
                        "source": gateway,
                        "target": target,
                        "relation": "pekko_cluster_communication",
                        "provenance": evidence,
                        "impact_direction": "bidirectional",
                        "confidence": 1.0,
                        "evidence_type": "documented",
                        "runtime_observed": False,
                    })
        things = role_nodes.get("Things")
        search = role_nodes.get("Things-Search")
        if things and search:
            edges.append({
                "source": things,
                "target": search,
                "relation": "documented_search_projection",
                "provenance": "project_plan:p2_topology",
                "impact_direction": "forward",
                "confidence": 0.8,
                "evidence_type": "documented",
                "runtime_observed": False,
            })
        database = next((node["id"] for node in nodes if node.get("kind") == "database"), None)
        if database:
            for role in ("Policies", "Things", "Things-Search", "Connectivity"):
                source = role_nodes.get(role)
                if source:
                    edges.append({
                        "source": source,
                        "target": database,
                        "relation": "documented_persistence",
                        "provenance": evidence,
                        "impact_direction": "reverse",
                        "confidence": 1.0,
                        "evidence_type": "documented",
                        "runtime_observed": False,
                    })
        return edges

    @staticmethod
    def _impact_paths(component_ids: set[str], relationships: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        dependents: dict[str, list[tuple[str, dict[str, Any]]]] = {component_id: [] for component_id in component_ids}
        for edge in relationships:
            if edge["source"] in component_ids and edge["target"] in component_ids:
                direction = edge.get("impact_direction", "none")
                if direction in {"reverse", "bidirectional"}:
                    dependents[edge["target"]].append((edge["source"], edge))
                if direction in {"forward", "bidirectional"}:
                    dependents[edge["source"]].append((edge["target"], edge))
        for edges in dependents.values():
            edges.sort(key=lambda item: (item[0], item[1]["relation"], item[1]["provenance"]))

        output: dict[str, list[dict[str, Any]]] = {}
        for target in sorted(component_ids):
            paths = [{"impacted_component": target, "path": [target], "edges": []}]
            visited = {target}
            frontier = [(target, [target], [])]
            while frontier:
                current, path, path_edges = frontier.pop(0)
                for dependent, edge in dependents[current]:
                    if dependent in visited:
                        continue
                    visited.add(dependent)
                    next_path = [*path, dependent]
                    next_edges = [*path_edges, edge]
                    paths.append({"impacted_component": dependent, "path": next_path, "edges": next_edges})
                    frontier.append((dependent, next_path, next_edges))
            output[target] = paths
        return output