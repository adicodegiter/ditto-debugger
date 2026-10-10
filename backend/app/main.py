from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import workbench_views as views
from .orchestrator import AgentOrchestrator
from .settings import REPOSITORY_ROOT, Settings
from safety.runner import RunnerError


class PlanRequest(BaseModel):
    baseline_id: str = Field(min_length=1, max_length=100)
    max_candidates: int = Field(default=3, ge=1, le=10)


class ApprovalRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=500)
    candidate_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


def create_app(settings: Settings | None = None, orchestrator: AgentOrchestrator | None = None) -> FastAPI:
    application = FastAPI(
        title="Ditto AI Chaos Engineering Workbench",
        version="1.0.0",
        description="Local-only, evidence-backed discovery and experiment planning. Fault execution is disabled in this build.",
    )
    application.state.orchestrator = orchestrator or AgentOrchestrator(settings or Settings.from_environment())

    @application.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "execution_enabled": application.state.orchestrator.settings.execution_enabled,
            "agents": application.state.orchestrator.agent_status(),
            "notes": ["Health reports workbench process state only; it does not imply Ditto or Prometheus is reachable."],
        }

    @application.get("/api/v1/agents")
    def agents() -> dict[str, Any]:
        return {"agents": application.state.orchestrator.agent_status()}

    @application.post("/api/v1/discovery/refresh")
    def refresh_discovery() -> dict[str, Any]:
        try:
            return application.state.orchestrator.discover_architecture()
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @application.get("/api/v1/architecture")
    def architecture() -> dict[str, Any]:
        try:
            return application.state.orchestrator.discover_architecture()
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @application.get("/api/v1/telemetry")
    def telemetry() -> dict[str, Any]:
        return application.state.orchestrator.collect_telemetry()

    @application.post("/api/v1/baselines")
    def capture_baseline() -> dict[str, Any]:
        try:
            return application.state.orchestrator.capture_baseline()
        except ValueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @application.post("/api/v1/experiments/plan", status_code=201)
    def plan_experiments(request: PlanRequest) -> dict[str, Any]:
        try:
            return application.state.orchestrator.plan_experiments(request.baseline_id, request.max_candidates)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @application.get("/api/v1/experiments")
    def list_experiments(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        return {"plans": application.state.orchestrator.list_plans(limit)}

    @application.get("/api/v1/experiments/{experiment_id}")
    def get_experiment(experiment_id: str) -> dict[str, Any]:
        experiment = application.state.orchestrator.get_experiment(experiment_id)
        if experiment is None:
            raise HTTPException(status_code=404, detail="Experiment was not found.")
        return experiment

    @application.get("/api/v1/experiments/{experiment_id}/status")
    def experiment_status(experiment_id: str) -> dict[str, Any]:
        experiment = application.state.orchestrator.get_experiment(experiment_id)
        if experiment is None:
            raise HTTPException(status_code=404, detail="Experiment was not found.")
        return {
            "experiment_id": experiment_id,
            "status": experiment["approval_status"],
            "approval": experiment["approval"],
            "execution_enabled": application.state.orchestrator.settings.execution_enabled,
        }

    @application.post("/api/v1/experiments/{experiment_id}/approval", status_code=201)
    def record_approval(experiment_id: str, request: ApprovalRequest) -> dict[str, Any]:
        try:
            return application.state.orchestrator.record_approval(
                experiment_id, request.decision, request.reason, request.candidate_hash
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/api/v1/runs")
    def list_runs(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        return {"runs": application.state.orchestrator.list_runs(limit)}

    @application.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        run = application.state.orchestrator.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run was not found.")
        return run

    @application.get("/api/v1/findings")
    def list_findings(run_id: str | None = None) -> dict[str, Any]:
        if run_id and application.state.orchestrator.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run was not found.")
        return {"findings": application.state.orchestrator.list_findings(run_id)}

    @application.get("/api/v1/findings/ranked")
    def ranked_findings() -> dict[str, Any]:
        return {"ranked": application.state.orchestrator.ranked_findings()}

    @application.get("/api/v1/audit")
    def list_audit(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
        return {"events": application.state.orchestrator.list_audit(limit)}

    # ---- read-only dashboard views (local files only; nothing executes) ----
    @application.get("/api/v1/slis")
    def current_slis(window: float = Query(default=30.0, ge=5, le=600)) -> dict[str, Any]:
        return views.sli_window(application.state.orchestrator.settings.metrics_dir, window)

    @application.get("/api/v1/slis/series")
    def sli_series(minutes: float = Query(default=5.0, ge=1, le=60), bucket: float = Query(default=10.0, ge=2, le=300)) -> dict[str, Any]:
        return views.sli_series(application.state.orchestrator.settings.metrics_dir, minutes, bucket)

    @application.get("/api/v1/baselines")
    def list_baselines(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
        stored = application.state.orchestrator.repository.list_baselines(limit)
        return {"baselines": [views.baseline_view(item) for item in stored]}

    @application.get("/api/v1/policy")
    def policy() -> dict[str, Any]:
        settings = application.state.orchestrator.settings
        return views.policy_summary(settings.policy_path, settings.execution_enabled)

    @application.get("/api/v1/dependency-map")
    def dependency_map() -> dict[str, Any]:
        return views.dependency_map(REPOSITORY_ROOT / "ai" / "dependency_map.json")

    @application.get("/api/v1/watchdog")
    def watchdog(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
        return {"events": views.watchdog_log(REPOSITORY_ROOT / "data" / "experiments" / "watchdog.jsonl", limit)}

    @application.post("/api/v1/experiments/{experiment_id}/execute")
    def execute_experiment(experiment_id: str) -> dict[str, Any]:
        try:
            return application.state.orchestrator.execute_experiment(experiment_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except RunnerError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @application.get("/")
    def dashboard() -> FileResponse:
        return FileResponse(REPOSITORY_ROOT / "frontend" / "index.html")

    application.mount("/assets", StaticFiles(directory=REPOSITORY_ROOT / "frontend"), name="assets")
    return application


app = create_app()