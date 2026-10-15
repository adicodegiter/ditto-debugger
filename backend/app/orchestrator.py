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
from ai.compound import compose_compound, interaction_effect, interaction_finding, suggest_pairs
from ai.diagnosis import diagnose, history_outcome
from ai.explain import explain_findings
from ai.ranking import rank_findings
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
        self.telemetry_agent = TelemetryObservabilityAgent(settings.prometheus_url, allow_remote=settings.prometheus_allow_remote)
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
            # Simulated samples are only acceptable when the operator explicitly chose
            # WORKLOAD_MODE=simulated; the stored baseline is stamped as simulated either way.
            allow_simulated=self.settings.workload_mode == "simulated",
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

    def autonomous_advisory_cycle(self, max_candidates: int = 3) -> dict[str, Any]:
        """One self-driven advisory pass: discover (Agent 1) -> measure (Agent 2) -> plan (Agent 3).

        This is the autonomous half of the workflow. It re-runs the three advisory agents on a
        schedule and keeps a fresh, ranked candidate set ready for a human. It is deliberately
        NON-persisting and NON-executing: it never saves a plan, never records an approval and
        never touches a container. Fault injection stays behind typed human approval and the
        operator CLI. Any agent that cannot run degrades to a reported status instead of raising.
        """
        from datetime import datetime, timezone

        cycle: dict[str, Any] = {
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "steps": {},
            "executes": False,
        }
        # Agent 1: discover the current architecture from the manifests.
        try:
            graph = self.discover_architecture()
            cycle["steps"]["discover"] = {
                "status": graph.get("status"),
                "graph_version": graph.get("graph_version"),
                "components": len(graph.get("nodes", [])),
            }
        except (OSError, ValueError) as error:
            cycle["steps"]["discover"] = {"status": "error", "detail": str(error)}
            return cycle  # without a graph there is nothing to measure or plan against
        # Agent 2: measure current telemetry (degrades to "unavailable" if Prometheus is unreachable).
        try:
            telemetry = self.collect_telemetry()
            cycle["steps"]["measure"] = {"status": telemetry.get("status"), "coverage": telemetry.get("coverage")}
        except (OSError, ValueError) as error:
            cycle["steps"]["measure"] = {"status": "error", "detail": str(error)}
        # Agent 3: rank candidates against the latest stored baseline. Preview only: nothing is saved.
        try:
            stored = self.repository.list_baselines(1)
            baseline = stored[0] if stored else None
            if baseline is None:
                cycle["steps"]["plan"] = {"status": "waiting_for_baseline", "detail": "No baseline stored yet."}
            else:
                plan = self.planning_agent.plan(graph, baseline["telemetry"], baseline, max_candidates)
                cycle["steps"]["plan"] = {
                    "status": "ok",
                    "baseline_id": baseline["baseline_id"],
                    "ranking_method": plan.get("ranking_method"),
                    "candidates": [
                        {
                            "id": candidate.get("id"),
                            "target_component": candidate.get("target_component"),
                            "action": candidate.get("action"),
                            "risk": candidate.get("risk_score"),
                        }
                        for candidate in plan.get("candidates", [])
                    ],
                }
        except ValueError as error:
            cycle["steps"]["plan"] = {"status": "blocked", "detail": str(error)}
        return cycle

    def compose_compound(self, first_id: str, second_id: str, stagger_seconds: int) -> dict[str, Any]:
        """Combine two planned single-fault experiments into one immutable compound candidate.

        Deterministic and non-executing: the candidate is policy-validated, stored as its own
        plan, and still needs a typed human approval like any other experiment.
        """
        first, second = self.repository.get_experiment(first_id), self.repository.get_experiment(second_id)
        if first is None or second is None:
            raise LookupError("Both experiments must exist in a stored plan.")
        candidate = compose_compound(first, second, stagger_seconds)
        self.policy.validate_compound(candidate)  # raises SafetyViolation for a forbidden combination
        digest = candidate["id"].removeprefix("compound-")
        return self.repository.save_plan({
            "schema_version": "1.0",
            "plan_id": f"plan-compound-{digest}",
            "ranking_method": candidate["ranking_method"],
            "candidates": [candidate],
            "policy_rejections": [],
            "execution_enabled": False,
        })

    def suggest_compounds(self, baseline_id: str, stagger_seconds: int = 10) -> list[dict[str, Any]]:
        plan = self.plan_experiments(baseline_id, 10)
        return suggest_pairs(plan["candidates"], self.policy.compound.allowed_pairs, stagger_seconds)

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
            if candidate.get("kind") == "compound":
                outcome = None  # planner history is per single target/action
            if outcome is not None:
                self.repository.record_outcome(candidate["target_component"], candidate["action"], outcome, f"run:{run['run_id']}")
            run["history_outcome"] = outcome
        findings = generate_findings(run)
        if run.get("kind") == "compound" and not run.get("dry_run") and not run.get("forced_test"):
            solo = [item for item in self.repository.list_runs(500) if item.get("kind") != "compound"]
            run["interaction"] = interaction_effect(run, solo)
            extra = interaction_finding(run, run["interaction"])
            if extra is not None:
                findings.append(extra)
        findings = explain_findings(findings, run)  # no-op unless AI_PROVIDER=groq
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
            {"id": "experiment_planning", "name": self.planning_agent.name,
             "status": "ready" if self.repository.list_baselines(1) else "blocked_until_baseline"},
        ]