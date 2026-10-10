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


def export_topology(
    compose_file: Path,
    inventory_path: Path,
    graph_path: Path,
    policy_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    report = DittoDiscoveryAgent(compose_file).discover()
    graph = ArchitectureMapperAgent().map(report)
    inventory = report.to_dict()
    discovered_ids = {component["id"] for component in inventory["components"]}
    missing_targets = sorted(_policy_targets(policy_path) - discovered_ids)
    if missing_targets:
        raise ValueError(f"Policy targets are absent from the discovered manifest: {', '.join(missing_targets)}")
    if graph["status"] != "available":
        raise ValueError(f"Manifest discovery did not produce a complete graph: {graph['status']}")
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
    args = parser.parse_args(argv)
    try:
        inventory, graph = export_topology(args.compose_file, args.inventory, args.graph, args.policy_file)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"Export failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"inventory": args.inventory.as_posix(), "graph": args.graph.as_posix(), "components": len(inventory["components"]), "edges": len(graph["edges"]), "graph_version": graph["graph_version"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())