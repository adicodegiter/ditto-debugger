"""Export the evidence-backed M1 inventory and dependency graph."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from app.agents.discovery import DittoDiscoveryAgent  # noqa: E402
from app.agents.mapping import ArchitectureMapperAgent  # noqa: E402


def _policy_targets(policy_path: Path) -> set[str]:
    raw = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    scope = raw.get("scope", {})
    targets = scope.get("allowed_targets", [])
    return {str(target) for target in targets} if isinstance(targets, list) else set()


DEFAULT_ENV_FILE = REPOSITORY_ROOT / "deploy" / "image-pins.env"

# The three user-facing SLIs from CLAUDE.md and the Ditto services on their request paths
# (Ditto architecture overview: nginx fronts the gateway; the gateway authorizes via
# policies, routes thing commands to things and searches to things-search; both persist
# in MongoDB). Added as labels.workbench_slis next to the mapping agent's affected_slis.
SLI_PATHS: dict[str, list[str]] = {
    "write": ["nginx", "gateway", "policies", "things", "mongodb"],
    "search": ["nginx", "gateway", "policies", "things-search", "mongodb"],
    "freshness": ["nginx", "gateway", "policies", "things", "mongodb"],
}

# The workbench's own components and calls. They are not discovered from a manifest, so
# every one is labelled manual (provenance_type "manual", evidence_type "operator").
WORKBENCH_NODES = [
    ("fleet-workload", "load_generator", "simulator/fleet_workload.py", "writes vehicle telemetry; probes write, search and freshness (SSE) SLIs"),
    ("workbench-api", "support", "backend/app", "discovery, mapping, baseline, planning, approval records; never executes faults"),
    ("operator-cli", "support", "safety/cli.py", "runner: steady-state check, live kill switch, rollback, recovery check"),
    ("watchdog", "support", "safety/watchdog.py", "independent max-duration cleanup, even if the runner dies"),
    ("data-metrics", "support", "data/metrics/*.jsonl", "SLI samples (JSON Lines) read by baseline and kill switch"),
]
WORKBENCH_EDGES = [
    ("fleet-workload", "nginx", "http_telemetry_write", "write SLI: PUT /api/2/things/{id}/features/telemetry/properties"),
    ("fleet-workload", "nginx", "http_search_query", "search SLI: GET /api/2/search/things"),
    ("fleet-workload", "nginx", "sse_subscription", "freshness SLI: SSE stream of telemetry changes"),
    ("fleet-workload", "data-metrics", "writes_samples", "append SLI samples"),
    ("workbench-api", "data-metrics", "reads_samples", "baseline from the latest live run"),
    ("operator-cli", "data-metrics", "reads_samples", "kill-switch window (last 30 s)"),
    ("operator-cli", "nginx", "http_recovery_check", "/health and an authenticated canary"),
    ("operator-cli", "watchdog", "arms_watchdog", "armed before injection, disarmed after clean rollback"),
    ("operator-cli", "things-search", "docker_compose_action", "allow-listed fault action and rollback"),
    ("operator-cli", "gateway", "docker_compose_action", "allow-listed fault action and rollback"),
    ("watchdog", "things-search", "docker_compose_start", "deadline cleanup"),
    ("watchdog", "gateway", "docker_compose_start", "deadline cleanup"),
]


def add_workbench_layer(graph: dict[str, Any]) -> dict[str, Any]:
    """Add CLAUDE.md SLI tags to Ditto nodes and the workbench's manual nodes and edges."""
    for node in graph["nodes"]:
        slis = sorted(sli for sli, path in SLI_PATHS.items() if node["id"] in path)
        if slis:
            node.setdefault("labels", {})["workbench_slis"] = ",".join(slis)
    known = {node["id"] for node in graph["nodes"]}
    for node_id, kind, module, purpose in WORKBENCH_NODES:
        if node_id in known:
            continue
        graph["nodes"].append({
            "schema_version": "1.0",
            "id": node_id,
            "name": node_id,
            "kind": kind,
            "source": "operator",
            "confidence": 1.0,
            "image": None,
            "ports": [],
            "health_checks": [],
            "labels": {"provenance_type": "manual", "module": module, "purpose": purpose},
            "affected_slis": [],
        })
    for source, target, relation, purpose in WORKBENCH_EDGES:
        graph["edges"].append({
            "source": source,
            "target": target,
            "relation": relation,
            "provenance": f"manual: {purpose}",
            "provenance_type": "manual",
            "impact_direction": "none",
            "confidence": 1.0,
            "evidence_type": "operator",
            "runtime_observed": False,
        })
    return graph


def export_topology(
    compose_file: Path,
    inventory_path: Path,
    graph_path: Path,
    policy_path: Path,
    env_files: tuple[Path, ...] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    # env_files: Compose --env-file inputs (deploy/image-pins.env) so image tags and ports
    # resolve to the values `docker compose` actually uses.
    report = DittoDiscoveryAgent(compose_file, env_files=env_files).discover()
    graph = ArchitectureMapperAgent().map(report)
    inventory = report.to_dict()
    discovered_ids = {component["id"] for component in inventory["components"]}
    missing_targets = sorted(_policy_targets(policy_path) - discovered_ids)
    if missing_targets:
        raise ValueError(f"Policy targets are absent from the discovered manifest: {', '.join(missing_targets)}")
    if graph["status"] != "available":
        raise ValueError(f"Manifest discovery did not produce a complete graph: {graph['status']}")
    graph = add_workbench_layer(graph)
    for path, payload in ((inventory_path, inventory), (graph_path, graph)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return inventory, graph


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the local Ditto inventory and dependency graph.")
    parser.add_argument("--compose-file", type=Path, default=REPOSITORY_ROOT / "deploy" / "ditto" / "docker-compose.yml")
    parser.add_argument("--policy-file", type=Path, default=REPOSITORY_ROOT / "configs" / "experiment-policy.yaml")
    parser.add_argument("--inventory", type=Path, default=REPOSITORY_ROOT / "data" / "inventory.json")
    parser.add_argument("--graph", type=Path, default=REPOSITORY_ROOT / "ai" / "dependency_map.json")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="Compose --env-file with the image pins")
    args = parser.parse_args(argv)
    env_files = (args.env_file,) if args.env_file and args.env_file.is_file() else ()
    try:
        inventory, graph = export_topology(args.compose_file, args.inventory, args.graph, args.policy_file, env_files)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"Export failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"inventory": args.inventory.as_posix(), "graph": args.graph.as_posix(), "components": len(inventory["components"]), "edges": len(graph["edges"]), "graph_version": graph["graph_version"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())