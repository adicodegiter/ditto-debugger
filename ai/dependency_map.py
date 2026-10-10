"""Generate the dependency map (M1): ai/dependency_map.json and docs/dependency-map.md.

Ditto nodes and edges come from the discovery and mapping agents, which read the pinned
compose file plus image pins without executing anything; each keeps its provenance
(configured / documented). The workbench's own components and their calls are added here
and labelled ``manual``. Each node carries the SLIs it can affect, derived from Ditto's
documented request paths (also labelled ``documented``).

    python -m ai.dependency_map            # regenerate both files
    python -m ai.dependency_map --check    # exit 1 if the committed files are out of date
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "backend"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from app.orchestrator import AgentOrchestrator  # noqa: E402
from app.settings import Settings  # noqa: E402

JSON_PATH = ROOT / "ai" / "dependency_map.json"
DOC_PATH = ROOT / "docs" / "dependency-map.md"

# Request paths per SLI through the Ditto services (Ditto architecture overview: nginx
# fronts the gateway; the gateway authorizes via policies and routes thing commands to
# things and search queries to things-search; things and things-search persist in MongoDB).
SLI_PATHS: dict[str, dict[str, Any]] = {
    "write": {
        "path": ["nginx", "gateway", "policies", "things", "mongodb"],
        "measured_by": "simulator/fleet_workload.py (PUT /api/2/things/{id}/features/telemetry/properties)",
    },
    "search": {
        "path": ["nginx", "gateway", "policies", "things-search", "mongodb"],
        "measured_by": "simulator/fleet_workload.py (GET /api/2/search/things)",
    },
    "freshness": {
        "path": ["nginx", "gateway", "policies", "things", "mongodb"],
        "measured_by": "simulator/fleet_workload.py (telemetry timestamp read back; SSE when enabled)",
    },
}

MANUAL_NODES = [
    {"id": "fleet-workload", "kind": "workbench", "name": "Fleet workload + SLI probes", "module": "simulator/fleet_workload.py",
     "purpose": "Writes vehicle telemetry and probes the write, search and freshness SLIs into data/metrics/*.jsonl"},
    {"id": "workbench-api", "kind": "workbench", "name": "Workbench API + agents", "module": "backend/app",
     "purpose": "Discovery, mapping, baseline, planning, approval records (never executes faults)"},
    {"id": "operator-cli", "kind": "control_plane", "name": "Operator CLI + runner + kill switch", "module": "safety/",
     "purpose": "Runs approved experiments with steady-state check, live kill switch, rollback and recovery check"},
    {"id": "watchdog", "kind": "control_plane", "name": "Independent watchdog", "module": "safety/watchdog.py",
     "purpose": "Restarts the target at the deadline even if the runner dies"},
    {"id": "docker-compose", "kind": "platform", "name": "Docker Compose (project 'ditto')", "module": "deploy/ditto",
     "purpose": "Container lifecycle for the pinned stack; the only fault-injection surface"},
]

MANUAL_EDGES = [
    ("fleet-workload", "nginx", "http", "telemetry writes (write SLI)"),
    ("fleet-workload", "nginx", "http", "fleet search queries (search SLI)"),
    ("fleet-workload", "nginx", "http/sse", "read back telemetry timestamps (freshness SLI)"),
    ("workbench-api", "data-metrics", "file", "baseline from SLI samples"),
    ("fleet-workload", "data-metrics", "file", "append SLI samples (JSON Lines)"),
    ("operator-cli", "data-metrics", "file", "kill-switch observations (last 30 s window)"),
    ("operator-cli", "docker-compose", "cli", "preflight, fixed fault action, rollback (allow-listed targets only)"),
    ("operator-cli", "watchdog", "process", "arm before injection, disarm after clean rollback"),
    ("watchdog", "docker-compose", "cli", "deadline cleanup: start target"),
    ("operator-cli", "nginx", "http", "recovery check: /health + authenticated canary"),
    ("docker-compose", "things-search", "cli", "restart/stop target (experiments)"),
    ("docker-compose", "gateway", "cli", "restart/stop target (experiments)"),
]


def _relative(path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _slis_for(node_id: str) -> list[str]:
    return sorted(sli for sli, spec in SLI_PATHS.items() if node_id in spec["path"])


def build(settings: Settings) -> dict[str, Any]:
    orchestrator = AgentOrchestrator(settings)
    graph = orchestrator.discover_architecture()
    if graph.get("status") != "available":
        raise SystemExit("No deployment manifest discovered; refusing to write a fabricated dependency map.")
    nodes: list[dict[str, Any]] = []
    for node in sorted(graph["nodes"], key=lambda item: item["id"]):
        nodes.append({
            "id": node["id"],
            "kind": node["kind"],
            "image": node.get("image"),
            "ports": [f"{port['host_ip']}:{port['host_port']}->{port['container_port']}/{port['protocol']}"
                      for port in node.get("ports", []) if port.get("host_port")],
            "health": node.get("health_url"),
            "slis": _slis_for(node["id"]),
            "provenance_type": "configured",
            "provenance": f"compose:{node['id']}",
        })
    for node in MANUAL_NODES:
        nodes.append({**node, "slis": [], "provenance_type": "manual", "provenance": "manual: workbench component"})
    nodes.append({"id": "data-metrics", "kind": "storage", "name": "data/metrics/*.jsonl", "slis": [],
                  "provenance_type": "manual", "provenance": "manual: workbench component"})

    edges: list[dict[str, Any]] = []
    for edge in sorted(graph["edges"], key=lambda item: (item["source"], item["target"], item["relation"])):
        edges.append({
            "source": edge["source"],
            "target": edge["target"],
            "relation": edge["relation"],
            "provenance_type": edge.get("provenance_type"),
            "provenance": edge.get("provenance"),
            "impact_direction": edge.get("impact_direction"),
            "runtime_observed": edge.get("runtime_observed", False),
        })
    for source, target, protocol, purpose in MANUAL_EDGES:
        edges.append({"source": source, "target": target, "relation": "calls", "protocol": protocol, "purpose": purpose,
                      "provenance_type": "manual", "provenance": "manual: workbench design", "runtime_observed": False})

    return {
        "schema_version": "1.0",
        "graph_version": graph.get("graph_version"),
        "sources": [_relative(settings.compose_file), _relative(settings.image_pins_file)],
        "sli_paths": {sli: {**spec, "provenance_type": "documented", "provenance": "Ditto architecture overview: request routing"}
                      for sli, spec in SLI_PATHS.items()},
        "nodes": nodes,
        "edges": edges,
        "notes": [
            "Ditto edges are static (configured from compose or documented by Ditto); none is runtime-observed yet.",
            "Docker network membership alone is not treated as an application call.",
            "manual = workbench components and calls written by this team, not discovered from a manifest.",
        ],
    }


def to_mermaid(dependency_map: dict[str, Any]) -> str:
    def node_id(value: str) -> str:
        return value.replace("-", "_")

    lines = ["flowchart LR"]
    groups = {"ditto": [], "workbench": []}
    for node in dependency_map["nodes"]:
        label = node["id"]
        if node["slis"]:
            label += "<br/><small>" + ", ".join(node["slis"]) + "</small>"
        groups["ditto" if node["provenance_type"] != "manual" else "workbench"].append(f'    {node_id(node["id"])}["{label}"]')
    lines.append('  subgraph ditto["Ditto 3.9.8 (from compose)"]')
    lines += groups["ditto"]
    lines.append("  end")
    lines.append('  subgraph workbench["Workbench (manual)"]')
    lines += groups["workbench"]
    lines.append("  end")
    seen: set[tuple[str, str, str]] = set()
    for edge in dependency_map["edges"]:
        style = {"configured": "-->", "documented": "-.->", "manual": "==>"}.get(edge["provenance_type"], "-->")
        label = edge.get("protocol") or edge["relation"].replace("_", " ")
        key = (edge["source"], edge["target"], label)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f'  {node_id(edge["source"])} {style}|{label}| {node_id(edge["target"])}')
    return "\n".join(lines)


def to_markdown(dependency_map: dict[str, Any]) -> str:
    rows = "\n".join(
        f"| `{node['id']}` | {node['kind']} | {', '.join(node['slis']) or '—'} | {node['provenance_type']} |"
        for node in dependency_map["nodes"]
    )
    paths = "\n".join(
        f"| {sli} | {' → '.join(spec['path'])} | {spec['measured_by']} |" for sli, spec in dependency_map["sli_paths"].items()
    )
    return f"""# Dependency Map

Generated by `python -m ai.dependency_map` from the pinned compose file and image pins. Do not edit by hand.
Machine-readable version: [`ai/dependency_map.json`](../ai/dependency_map.json).

Edge styles: solid = **configured** (compose), dotted = **documented** (Ditto architecture), thick = **manual** (workbench components).

```mermaid
{to_mermaid(dependency_map)}
```

## Which SLI depends on which service

| SLI | Request path | Measured by |
|---|---|---|
{paths}

## Nodes

| Node | Kind | SLIs it can affect | Provenance |
|---|---|---|---|
{rows}

{chr(10).join('- ' + note for note in dependency_map['notes'])}
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed files are out of date")
    args = parser.parse_args(argv)
    dependency_map = build(Settings.from_environment())
    json_text = json.dumps(dependency_map, indent=2) + "\n"
    doc_text = to_markdown(dependency_map)
    if args.check:
        current = (JSON_PATH.read_text(encoding="utf-8") if JSON_PATH.is_file() else "", DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.is_file() else "")
        if current != (json_text, doc_text):
            print("Dependency map is out of date: run python -m ai.dependency_map", file=sys.stderr)
            return 1
        print("Dependency map is up to date.")
        return 0
    JSON_PATH.write_text(json_text, encoding="utf-8", newline="\n")
    DOC_PATH.write_text(doc_text, encoding="utf-8", newline="\n")
    print(f"Wrote {JSON_PATH.relative_to(ROOT)} ({len(dependency_map['nodes'])} nodes, {len(dependency_map['edges'])} edges) and {DOC_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
