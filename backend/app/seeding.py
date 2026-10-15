"""Seed an empty store with the committed, real recorded runs (docs/evidence/**).

A deployment with no Docker daemon (e.g. Render) can never execute a fault, so its Runs page
would always be empty. The runs imported here are REAL records the operator CLI executed on the
documented evaluation hosts (see docs/m4-results.md); they are committed evidence, imported
verbatim into an EMPTY store and stamped with ``seeded_evidence`` (their source path) so the UI
can label them. Dry runs and forced kill-switch tests are excluded, exactly like the scoring
rules. This never executes anything and never touches a store that already has runs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .store import LocalRepository


def seed_recorded_runs(repository: LocalRepository, repo_root: Path) -> dict[str, Any]:
    """Import committed evidence runs into an empty store. Idempotent; read-only towards Docker."""
    if repository.list_runs(1):
        return {"seeded": 0, "reason": "store_not_empty", "run_ids": []}
    seeded: list[str] = []
    evidence_root = repo_root / "docs" / "evidence"
    for path in sorted(evidence_root.glob("**/run-*.json")):
        if "samples" in path.parts:
            continue
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(run, dict) or not isinstance(run.get("run_id"), str):
            continue
        if run.get("dry_run") or run.get("forced_test"):
            continue
        if repository.get_run(run["run_id"]) is not None:
            continue
        run["seeded_evidence"] = path.relative_to(repo_root).as_posix()
        repository.save_run(run)
        seeded.append(run["run_id"])
    return {"seeded": len(seeded), "run_ids": seeded}