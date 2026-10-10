"""Print and save the planner's ranked candidates.

  python -m ai.plan_cli --fixture                  offline: real compose graph + LABELLED FIXTURE telemetry
  python -m ai.plan_cli --baseline-id baseline-x   live: uses the running workbench's real baseline
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agents.discovery import DittoDiscoveryAgent  # noqa: E402
from app.agents.mapping import ArchitectureMapperAgent  # noqa: E402
from app.agents.planning import ExperimentPlanningAgent  # noqa: E402
from app.orchestrator import AgentOrchestrator  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.store import LocalRepository  # noqa: E402


def fixture_inputs(settings: Settings):
    graph = ArchitectureMapperAgent().map(DittoDiscoveryAgent(settings.compose_file).discover())
    telemetry = {"status": "available", "coverage": 1.0, "fixture": True,
                 "observations": [{"metric": "up", "quality": "fresh"}]}
    baseline = {"baseline_id": "FIXTURE-baseline", "captured_at": datetime.now(timezone.utc).isoformat(),
                "graph_version": graph.get("graph_version"), "telemetry_status": "available"}
    return graph, telemetry, baseline


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixture", action="store_true", help="offline demo; NOT a real baseline")
    ap.add_argument("--baseline-id", help="baseline id returned by POST /api/v1/baselines")
    ap.add_argument("--max-candidates", type=int, default=3)
    ap.add_argument("--out", default=str(ROOT / "data" / "plans"))
    a = ap.parse_args()
    settings = Settings.from_environment()
    if a.fixture:
        print("*** FIXTURE TELEMETRY: offline demo only, not a real baseline ***")
        graph, telemetry, baseline = fixture_inputs(settings)
        repo = LocalRepository(Path(tempfile.mkdtemp()) / "fixture.sqlite3")
        result = ExperimentPlanningAgent(settings.policy_path, repo).plan(graph, telemetry, baseline, a.max_candidates)
    elif a.baseline_id:
        result = AgentOrchestrator(settings).plan_experiments(a.baseline_id, a.max_candidates)
    else:
        ap.error("give --fixture or --baseline-id")
    for i, c in enumerate(result["candidates"], 1):
        print(f"{i}. {c['target_component']:14} {c['action']}  score={c['selection_score']:.3f}  predicted={c.get('predicted_slis')}")
        print(f"   reason: {c.get('reason')}")
    for r in result["policy_rejections"]:
        print(f"   not planned: {r['target']}: {r['reason']}")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{result['plan_id']}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("Saved:", path)


if __name__ == "__main__":
    main()
