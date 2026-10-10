from __future__ import annotations

from typing import Any

from .agents.discovery import DittoDiscoveryAgent
from .agents.mapping import ArchitectureMapperAgent
from .agents.planning import ExperimentPlanningAgent
from .agents.telemetry import TelemetryObservabilityAgent
from .settings import Settings
from .store import LocalRepository
from safety.policy import Policy
from safety.runner import RunnerError
from monitor.window import baseline_from_metrics
from ai.diagnosis import diagnose, history_outcome
from ai.report import generate_findings


# Keys the store adds to a candidate when reading it back. They are not part of the
# immutable proposal, so they must be removed before checking the approval hash.
_STORED_ENRICHMENT_KEYS = frozenset({"plan_id", "candidate_hash", "approval_status", "approval"})


class AgentOrchestrator:
    """Coordinates specialists; each agent returns evidence-bearing records."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        pins = (settings.image_pins_file,) if settings.image_pins_file.is_file() else ()
        self.discovery_agent = DittoDiscoveryAgent(settings.compose_file, settings.kubernetes_manifests, env_files=pins)
        self.mapping_agent = ArchitectureMapperAgent()
        self.telemetry_agent = TelemetryObservabilityAgent(settings.prometheus_url)
        self.repository = LocalRepository(settings.database_path)
        self.policy = Policy.from_file(settings.policy_path)
        self.planning_agent = ExperimentPlanningAgent(settings.policy_path, self.repository)

    def discover_architecture(self) -> dict[str, Any]:
        report = self.discovery_agent.discover()
        return self.mapping_agent.map(report)

    def collect_telemetry(self) -> dict[str, Any]:
        return self.telemetry_agent.collect()

    def capture_baseline(self) -> dict[str, Any]:
        graph = self.discover_architecture()
        if graph["status"] != "available":
            raise ValueError("No discovered deployment is available for a baseline.")
        telemetry = baseline_from_metrics(
            self.settings.metrics_dir,
            thresholds_file=self.settings.policy_path.parent / "telemetry.yaml",
        )
        if telemetry.get("status") != "available":
            reasons = telemetry.get("warnings") or ["telemetry is not healthy"]
            raise ValueError("Baseline refused: " + "; ".join(reasons))
        return self.repository.save_baseline(graph["graph_version"], telemetry)

    def plan_experiments(self, baseline_id: str, max_candidates: int) -> dict[str, Any]:
        graph = self.discover_architecture()
        baseline = self.repository.get_baseline(baseline_id)
        if baseline is None:
            raise LookupError("Baseline was not found.")
        telemetry = baseline["telemetry"]
        plan = self.planning_agent.plan(graph, telemetry, baseline, max_candidates)
        return self.repository.save_plan(plan)

    def list_plans(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.repository.list_plans(limit)

    def get_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        return self.repository.get_experiment(experiment_id)

    def record_approval(self, experiment_id: str, decision: str, reason: str, candidate_hash: str) -> dict[str, Any]:
        return self.repository.record_approval(
            experiment_id, decision, reason, candidate_hash, self.policy.approval_expires_after_minutes
        )

    def execute_experiment(self, experiment_id: str) -> dict[str, Any]:
        # The API process never runs Docker (team agreement: the runner has its own
        # process boundary). Approved experiments run via the operator CLI.
        raise RunnerError(
            "Fault execution is disabled in the API. Run approved experiments with the operator CLI: "
            "python -m safety.cli execute <experiment_id> [--dry-run]."
        )

    def prepare_execution(self, experiment_id: str) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
        """Return (immutable candidate, approval, baseline) for the operator CLI."""
        experiment = self.repository.get_experiment(experiment_id)
        if experiment is None:
            raise LookupError("Experiment was not found.")
        if experiment.get("approval_status") != "approved":
            raise RunnerError("Only an approved experiment can execute.")
        baseline_id = next((ref.removeprefix("baseline:") for ref in experiment.get("evidence_refs", []) if ref.startswith("baseline:")), "")
        baseline = self.repository.get_baseline(baseline_id)
        if baseline is None:
            raise RunnerError("The experiment has no executable baseline reference.")
        candidate = {key: value for key, value in experiment.items() if key not in _STORED_ENRICHMENT_KEYS}
        return candidate, experiment.get("approval"), baseline

    def diagnose_run(self, run: dict[str, Any], candidate: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any]:
        """Predicted vs observed for one run (deterministic, no LLM)."""
        return diagnose(candidate, run, baseline)

    def record_operator_run(
        self,
        run: dict[str, Any],
        candidate: dict[str, Any] | None = None,
        baseline: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Persist a real run from the operator CLI with its diagnosis, findings and planner history."""
        if candidate is not None:
            run["diagnosis"] = self.diagnose_run(run, candidate, baseline)
            outcome = history_outcome(run["diagnosis"])  # None for forced tests / dry runs / gaps
            if outcome is not None:
                self.repository.record_outcome(candidate["target_component"], candidate["action"], outcome, f"run:{run['run_id']}")
            run["history_outcome"] = outcome
        findings = generate_findings(run)
        self.repository.save_run(run)
        self.repository.save_findings(run["run_id"], findings)
        return findings

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.repository.list_runs(limit)

    def record_run(self, run: dict[str, Any]) -> None:
        self.repository.save_run(run)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.repository.get_run(run_id)

    def record_findings(self, run_id: str, findings: list[dict[str, Any]]) -> None:
        self.repository.save_findings(run_id, findings)

    def ranked_findings(self, limit: int = 200) -> list[dict[str, Any]]:
        return rank_findings(self.repository.list_runs(limit), self.repository.list_findings())

    def list_findings(self, run_id: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_findings(run_id)

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_audit(limit)

    def agent_status(self) -> list[dict[str, str]]:
        discovery = "ready" if self.settings.compose_file.is_file() or any(path.is_file() for path in self.settings.kubernetes_manifests) else "waiting_for_manifest"
        return [
            {"id": "ditto_discovery", "name": self.discovery_agent.name, "status": discovery},
            {"id": "architecture_mapping", "name": self.mapping_agent.name, "status": "ready"},
            {"id": "telemetry_observability", "name": self.telemetry_agent.name, "status": "ready_to_poll"},
            {"id": "experiment_planning", "name": self.planning_agent.name, "status": "blocked_until_baseline"},
        ]