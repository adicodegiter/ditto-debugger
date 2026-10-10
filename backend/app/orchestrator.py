from __future__ import annotations

from datetime import datetime, timezone
import base64
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .agents.discovery import DittoDiscoveryAgent
from .agents.mapping import ArchitectureMapperAgent
from .agents.planning import ExperimentPlanningAgent
from .agents.telemetry import TelemetryObservabilityAgent
from .settings import Settings
from .store import LocalRepository
from safety.policy import Policy
from safety.runner import ExperimentRunner, RunnerError, SubprocessDockerAdapter
from monitor.window import baseline_from_metrics, window_stats
from ai.report import generate_findings


class _ObservationProvider:
    def __init__(self, metrics_dir, baseline: dict[str, Any], thresholds_file) -> None:
        self.metrics_dir = metrics_dir
        self.baseline = baseline
        self.thresholds_file = thresholds_file

    def observe(self) -> dict[str, Any]:
        return window_stats(self.metrics_dir, last_s=30, baseline=self.baseline, thresholds_file=self.thresholds_file)


class _RecoveryChecker:
    def __init__(self, base_url: str, username: str | None = None, password: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_header = None
        if username and password:
            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            self.auth_header = f"Basic {token}"

    def verify(self, target: str) -> dict[str, Any]:
        canary_path = "/api/2/search/things?option=size(1)" if target == "things-search" else "/api/2/things?limit=1"
        started = datetime.now(timezone.utc).timestamp()
        deadline = started + 60
        while datetime.now(timezone.utc).timestamp() < deadline:
            try:
                health_headers = {"Accept": "*/*"}
                if self.auth_header:
                    health_headers["Authorization"] = self.auth_header
                with urlopen(Request(f"{self.base_url}/health", headers=health_headers), timeout=3) as response:
                    healthy = response.status == 200
                canary_headers = {"Accept": "application/json"}
                if self.auth_header:
                    canary_headers["Authorization"] = self.auth_header
                request = Request(f"{self.base_url}{canary_path}", headers=canary_headers)
                with urlopen(request, timeout=3) as response:
                    canary = response.status == 200
                if healthy and canary:
                    return {
                        "target": target,
                        "healthy": True,
                        "canary": True,
                        "recovery_time_seconds": datetime.now(timezone.utc).timestamp() - started,
                    }
            except (HTTPError, URLError, OSError):
                pass
            import time
            time.sleep(2)
        return {
            "target": target,
            "healthy": False,
            "canary": False,
            "error": "recovery_timeout",
            "recovery_time_seconds": datetime.now(timezone.utc).timestamp() - started,
        }


class AgentOrchestrator:
    """Coordinates specialists; each agent returns evidence-bearing records."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.discovery_agent = DittoDiscoveryAgent(settings.compose_file, settings.kubernetes_manifests)
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
        if not self.settings.execution_enabled:
            raise RunnerError("Fault execution is disabled. Set CHAOS_EXECUTION_ENABLED=true for the local runner.")
        experiment = self.repository.get_experiment(experiment_id)
        if experiment is None:
            raise LookupError("Experiment was not found.")
        if experiment.get("approval_status") != "approved":
            raise RunnerError("Only an approved experiment can execute.")
        baseline_id = next((ref.removeprefix("baseline:") for ref in experiment.get("evidence_refs", []) if ref.startswith("baseline:")), "")
        baseline = self.repository.get_baseline(baseline_id)
        if baseline is None:
            raise RunnerError("The experiment has no executable baseline reference.")
        runner = ExperimentRunner(
            self.policy,
            SubprocessDockerAdapter(),
            _ObservationProvider(
                self.settings.metrics_dir,
                baseline.get("telemetry", baseline),
                self.settings.policy_path.parent / "telemetry.yaml",
            ),
            _RecoveryChecker(
                self.settings.ditto_base_url,
                self.settings.ditto_username,
                self.settings.ditto_password,
            ),
            audit=self.repository.record_audit,
            consume_approval=self.repository.consume_approval,
        )
        run = runner.execute(experiment, experiment.get("approval"), baseline)
        self.repository.save_run(run)
        self.repository.save_findings(run["run_id"], generate_findings(run))
        return run

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.repository.list_runs(limit)

    def record_run(self, run: dict[str, Any]) -> None:
        self.repository.save_run(run)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.repository.get_run(run_id)

    def record_findings(self, run_id: str, findings: list[dict[str, Any]]) -> None:
        self.repository.save_findings(run_id, findings)

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