from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


class LocalRepository:
    """Stores only baseline summaries and explicit experiment outcome records."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS baselines ("
                "id TEXT PRIMARY KEY, captured_at TEXT NOT NULL, graph_version TEXT NOT NULL, "
                "telemetry_status TEXT NOT NULL, telemetry_json TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_outcomes ("
                "id TEXT PRIMARY KEY, target TEXT NOT NULL, action TEXT NOT NULL, outcome TEXT NOT NULL, "
                "evidence_ref TEXT NOT NULL, recorded_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_plans ("
                "id TEXT PRIMARY KEY, plan_json TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_approvals ("
                "experiment_id TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE, decision TEXT NOT NULL, reason TEXT NOT NULL, "
                "candidate_hash TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT)"
            )
            approval_columns = {row[1] for row in connection.execute("PRAGMA table_info(experiment_approvals)").fetchall()}
            if "consumed_at" not in approval_columns:
                connection.execute("ALTER TABLE experiment_approvals ADD COLUMN consumed_at TEXT")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_runs ("
                "id TEXT PRIMARY KEY, run_json TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS findings ("
                "id TEXT PRIMARY KEY, run_id TEXT NOT NULL, finding_json TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS audit_events ("
                "id TEXT PRIMARY KEY, event TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at TEXT NOT NULL)"
            )

    def save_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        created_at = datetime.now(timezone.utc).isoformat()
        stored_plan = {**plan, "created_at": created_at}
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT plan_json FROM experiment_plans WHERE id = ?", (plan["plan_id"],)
            ).fetchone()
            if existing:
                previous = json.loads(existing[0])
                candidate_ids = [candidate.get("id") for candidate in previous.get("candidates", [])]
                placeholders = ",".join("?" for _ in candidate_ids)
                has_approval = candidate_ids and connection.execute(
                    f"SELECT 1 FROM experiment_approvals WHERE experiment_id IN ({placeholders}) LIMIT 1",
                    candidate_ids,
                ).fetchone()
                if has_approval:
                    return previous
            connection.execute(
                "INSERT OR REPLACE INTO experiment_plans (id, plan_json, created_at) VALUES (?, ?, ?)",
                (plan["plan_id"], json.dumps(stored_plan), created_at),
            )
        return stored_plan

    def list_plans(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT plan_json FROM experiment_plans ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._attach_approval_state(json.loads(row[0])) for row in rows]

    def get_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT plan_json FROM experiment_plans ORDER BY created_at DESC"
            ).fetchall()
        for row in rows:
            plan = json.loads(row[0])
            for candidate in plan.get("candidates", []):
                if candidate.get("id") == experiment_id:
                    with self._connect() as connection:
                        approval = connection.execute(
                            "SELECT id, decision, reason, candidate_hash, created_at, expires_at "
                            "FROM experiment_approvals WHERE experiment_id = ?", (experiment_id,)
                        ).fetchone()
                    result = {**candidate, "plan_id": plan["plan_id"]}
                    result["candidate_hash"] = self._candidate_hash(candidate)
                    result["approval_status"] = (
                        "approved" if approval[1] == "approve" else "rejected"
                    ) if approval else "pending"
                    result["approval"] = self._approval_dict(approval) if approval else None
                    return result
        return None

    def record_approval(
        self, experiment_id: str, decision: str, reason: str, candidate_hash: str, expires_after_minutes: int = 60
    ) -> dict[str, Any]:
        experiment = self.get_experiment(experiment_id)
        if experiment is None:
            raise LookupError("Experiment was not found.")
        if experiment["candidate_hash"] != candidate_hash:
            raise ValueError("Candidate hash does not match the current immutable proposal.")
        if experiment["approval"] is not None:
            raise ValueError("An approval decision has already been recorded for this proposal.")
        created_at = datetime.now(timezone.utc)
        approval = {
            "approval_id": uuid.uuid4().hex,
            "decision": decision,
            "reason": reason,
            "candidate_hash": candidate_hash,
            "created_at": created_at.isoformat(),
            "expires_at": (created_at.replace(microsecond=0) + timedelta(minutes=expires_after_minutes)).isoformat(),
            "execution_enabled": False,
        }
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO experiment_approvals "
                "(experiment_id, id, decision, reason, candidate_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (experiment_id, approval["approval_id"], decision, reason, candidate_hash, approval["created_at"], approval["expires_at"]),
            )
        return approval

    def consume_approval(self, approval: dict[str, Any]) -> None:
        approval_id = approval.get("approval_id")
        candidate_hash = approval.get("candidate_hash")
        if not approval_id or not candidate_hash:
            raise ValueError("Approval identity is required for execution.")
        consumed_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE experiment_approvals SET consumed_at = ? "
                "WHERE id = ? AND candidate_hash = ? AND decision = 'approve' AND consumed_at IS NULL",
                (consumed_at, approval_id, candidate_hash),
            )
            if result.rowcount != 1:
                raise ValueError("Approval has already been consumed or is invalid.")

    def save_run(self, run: dict[str, Any]) -> None:
        run_id = run.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("A run_id is required.")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO experiment_runs (id, run_json, created_at) VALUES (?, ?, ?)",
                (run_id, json.dumps(run), run.get("created_at", datetime.now(timezone.utc).isoformat())),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT run_json FROM experiment_runs WHERE id = ?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_json FROM experiment_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_findings(self, run_id: str, findings: list[dict[str, Any]]) -> None:
        if self.get_run(run_id) is None:
            raise LookupError("Run was not found.")
        with self._connect() as connection:
            for finding in findings:
                if finding.get("run_id") != run_id:
                    raise ValueError("Finding run_id must match the requested run.")
                connection.execute(
                    "INSERT OR REPLACE INTO findings (id, run_id, finding_json) VALUES (?, ?, ?)",
                    (finding["id"], run_id, json.dumps(finding)),
                )

    def list_findings(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if run_id:
                rows = connection.execute(
                    "SELECT finding_json FROM findings WHERE run_id = ? ORDER BY id", (run_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT finding_json FROM findings ORDER BY id DESC").fetchall()
        return [json.loads(row[0]) for row in rows]

    def record_audit(self, event: str, payload: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_events (id, event, payload_json, recorded_at) VALUES (?, ?, ?, ?)",
                (uuid.uuid4().hex, event, json.dumps(payload), datetime.now(timezone.utc).isoformat()),
            )

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event, payload_json, recorded_at FROM audit_events ORDER BY recorded_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{"event": row[0], "payload": json.loads(row[1]), "recorded_at": row[2]} for row in rows]

    def save_baseline(self, graph_version: str, telemetry: dict[str, Any]) -> dict[str, Any]:
        if telemetry.get("status") != "available":
            raise ValueError("A baseline requires fresh, healthy telemetry for all scraped targets.")
        baseline = {
            "baseline_id": f"baseline-{uuid.uuid4().hex[:12]}",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "graph_version": graph_version,
            "telemetry_status": telemetry["status"],
            "telemetry": telemetry,
        }
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO baselines (id, captured_at, graph_version, telemetry_status, telemetry_json) VALUES (?, ?, ?, ?, ?)",
                (baseline["baseline_id"], baseline["captured_at"], graph_version, telemetry["status"], json.dumps(telemetry)),
            )
        return baseline

    def get_baseline(self, baseline_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, captured_at, graph_version, telemetry_status, telemetry_json FROM baselines WHERE id = ?",
                (baseline_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "baseline_id": row[0],
            "captured_at": row[1],
            "graph_version": row[2],
            "telemetry_status": row[3],
            "telemetry": json.loads(row[4]),
        }

    def record_outcome(self, target: str, action: str, outcome: str, evidence_ref: str) -> None:
        if outcome not in {"failure_observed", "no_failure_observed"}:
            raise ValueError("Only completed, conclusive outcomes can update planner history.")
        if not evidence_ref.strip():
            raise ValueError("An evidence reference is required to record an outcome.")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO experiment_outcomes (id, target, action, outcome, evidence_ref, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, target, action, outcome, evidence_ref, datetime.now(timezone.utc).isoformat()),
            )

    def history_for(self, target: str, action: str) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT outcome, COUNT(*) FROM experiment_outcomes WHERE target = ? AND action = ? GROUP BY outcome",
                (target, action),
            ).fetchall()
        counts = {outcome: count for outcome, count in rows}
        return {
            "trials": sum(counts.values()),
            "failures": counts.get("failure_observed", 0),
            "non_failures": counts.get("no_failure_observed", 0),
        }

    def _attach_approval_state(self, plan: dict[str, Any]) -> dict[str, Any]:
        candidates = []
        for candidate in plan.get("candidates", []):
            experiment = self.get_experiment(candidate["id"])
            candidates.append({**candidate, **{
                "candidate_hash": experiment["candidate_hash"],
                "approval_status": experiment["approval_status"],
                "approval": experiment["approval"],
            }})
        return {**plan, "candidates": candidates}

    @staticmethod
    def _candidate_hash(candidate: dict[str, Any]) -> str:
        payload = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _approval_dict(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "approval_id": row[0],
            "decision": row[1],
            "reason": row[2],
            "candidate_hash": row[3],
            "created_at": row[4],
            "expires_at": row[5],
            "execution_enabled": False,
        }

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=2.0)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()