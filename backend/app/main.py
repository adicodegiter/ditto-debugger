from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import workbench_views as views
from .graph import AgentGraph
from .orchestrator import AgentOrchestrator
from .settings import REPOSITORY_ROOT, Settings
from safety.runner import RunnerError


class PlanRequest(BaseModel):
    baseline_id: str = Field(min_length=1, max_length=100)
    max_candidates: int = Field(default=3, ge=1, le=10)


class AgentGraphRunRequest(BaseModel):
    max_candidates: int = Field(default=3, ge=1, le=10)
    baseline_id: str | None = Field(default=None, max_length=100)


class ApprovalRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=500)
    candidate_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


def _cors_allowed_origins() -> list[str]:
    """Browser origins permitted to call the API.

    Defaults cover the deployed dashboard plus the local dev origins. Override with the
    ``CORS_ALLOWED_ORIGINS`` environment variable (comma-separated) on Render. Using ``*`` is
    intentionally avoided so the typed human-approval actions stay origin-scoped.
    """
    raw = os.environ.get("CORS_ALLOWED_ORIGINS")
    if raw is not None:
        origins = [item.strip() for item in raw.split(",") if item.strip()]
        if origins:
            return origins
    return [
        "https://ditto-debugger.onrender.com",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]


def create_app(settings: Settings | None = None, orchestrator: AgentOrchestrator | None = None) -> FastAPI:
    application = FastAPI(
        title="Ditto AI Chaos Engineering Workbench",
        version="1.0.0",
        description="Autonomous advisory agents (discover -> measure -> plan) keep a ranked candidate set ready for a human, using a transparent, reproducible ranking. Fault execution is disabled in this build; faults run only from the operator CLI after a typed human approval.",
    )
    # The dashboard is served from a separate origin in production (Render static site -> this API),
    # so the browser requires explicit CORS headers on every response, including error responses.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_allowed_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
    application.state.orchestrator = orchestrator or AgentOrchestrator(settings or Settings.from_environment())

    # ---- autonomous advisory loop -------------------------------------------------------
    # The three advisory agents (discover -> measure -> plan) re-run on a schedule so a fresh,
    # ranked candidate set is always ready for a human. This loop is advisory only: it never
    # saves a plan, never records an approval and never executes a fault. Execution stays behind
    # typed human approval and the operator CLI. Off by default; enable with AUTONOMY_ENABLED=true.
    import threading

    autonomy = {
        "enabled": os.environ.get("AUTONOMY_ENABLED", "false").lower() == "true",
        "interval_seconds": float(os.environ.get("AUTONOMY_INTERVAL_SECONDS", "60") or 60),
        "last_cycle": None,
        "cycles": 0,
        "last_error": None,
        "executes": False,
    }
    application.state.autonomy = autonomy
    _stop = threading.Event()
    application.state.autonomy_stop = _stop

    def _autonomy_loop() -> None:
        while not _stop.wait(autonomy["interval_seconds"]):
            try:
                autonomy["last_cycle"] = application.state.orchestrator.autonomous_advisory_cycle()
                autonomy["cycles"] += 1
                autonomy["last_error"] = None
            except Exception as error:  # noqa: BLE001 - the loop must never crash the server
                autonomy["last_error"] = f"{type(error).__name__}: {error}"

    if autonomy["enabled"]:
        threading.Thread(target=_autonomy_loop, name="autonomous-advisory", daemon=True).start()

    @application.get("/api/v1/autonomy")
    def autonomy_status() -> dict[str, Any]:
        # Reports the autonomous advisory loop's state. It always reports executes=false: the loop
        # prepares candidates for a human; it never injects a fault.
        return {
            **autonomy,
            "note": "Autonomous advisory loop: discover -> measure -> plan on a schedule. Advisory only; it never saves an approval or executes. Faults run only from the operator CLI after a typed human approval.",
        }

    @application.post("/api/v1/autonomy/run")
    def autonomy_run() -> dict[str, Any]:
        # Run one advisory cycle on demand (same non-executing pass the scheduler runs).
        cycle = application.state.orchestrator.autonomous_advisory_cycle()
        autonomy["last_cycle"] = cycle
        autonomy["cycles"] += 1
        return cycle

    # ---- self-hosted fleet workload (drives live SLIs) --------------------------------
    # The dashboard's "live samples" come from data/metrics/*.jsonl, which the fleet workload
    # writes. On Render the API and the workload must share one filesystem, so the workload runs
    # INSIDE this service (not as a separate process). It needs a reachable Ditto (DITTO_BASE_URL);
    # with no Ditto it records an honest error and retries instead of fabricating samples.
    # Off by default; enable with WORKLOAD_ENABLED=true once Ditto is deployed and reachable.
    workload = {
        "enabled": os.environ.get("WORKLOAD_ENABLED", "false").lower() == "true",
        "mode": application.state.orchestrator.settings.workload_mode,
        "duration_seconds": float(os.environ.get("WORKLOAD_DURATION_SECONDS", "60") or 60),
        "vehicles": int(os.environ.get("WORKLOAD_VEHICLES", "4") or 4),
        "rate_per_second": float(os.environ.get("WORKLOAD_RATE_PER_SECOND", "1.0") or 1.0),
        "runs": 0,
        "last_status": "not_started",
        "last_error": None,
        "executes_faults": False,
    }
    application.state.workload = workload
    _workload_stop = threading.Event()
    application.state.workload_stop = _workload_stop

    def _workload_loop() -> None:
        # Simulated mode writes clearly labelled synthetic samples (no Ditto needed) so the
        # baseline -> plan -> approval flow works on a demo deployment. Live mode probes the real
        # Ditto. Both run in this thread; neither ever injects a fault.
        mode = application.state.orchestrator.settings.workload_mode
        while not _workload_stop.is_set():
            try:
                if mode == "simulated":
                    # Import lazily so a missing optional dependency never blocks API startup.
                    from simulator.simulated_workload import run_simulated_workload

                    summary = run_simulated_workload(
                        duration_seconds=workload["duration_seconds"],
                        vehicles=workload["vehicles"],
                        rate_per_second=workload["rate_per_second"],
                        metrics_dir=application.state.orchestrator.settings.metrics_dir,
                        stop_event=_workload_stop,
                    )
                else:
                    from simulator.fleet_workload import run_fleet_workload

                    summary = run_fleet_workload(
                        base_url=application.state.orchestrator.settings.ditto_base_url,
                        vehicles=workload["vehicles"],
                        duration_seconds=workload["duration_seconds"],
                        rate_per_second=workload["rate_per_second"],
                        metrics_dir=application.state.orchestrator.settings.metrics_dir,
                        capture_containers=False,  # no Docker daemon on a hosted service
                        freshness_method="poll-proxy",  # SSE needs the Ditto stream endpoint; poll is portable
                    )
                workload["last_status"] = summary.get("status", "unknown")
                workload["runs"] += 1
                workload["last_error"] = None
            except Exception as error:  # noqa: BLE001 - the loop must never crash the server
                workload["last_status"] = "error"
                workload["last_error"] = f"{type(error).__name__}: {error}"
                # Back off before retrying so a missing Ditto does not spin hot.
                _workload_stop.wait(15)

    if workload["enabled"]:
        threading.Thread(target=_workload_loop, name="fleet-workload", daemon=True).start()

    @application.get("/api/v1/workload")
    def workload_status() -> dict[str, Any]:
        # Reports the self-hosted fleet workload. Live mode only generates healthy twin traffic
        # against a reachable Ditto; simulated mode writes labelled synthetic samples. Neither
        # ever injects a fault.
        simulated = application.state.orchestrator.settings.workload_mode == "simulated"
        return {
            **workload,
            "ditto_base_url": None if simulated else application.state.orchestrator.settings.ditto_base_url,
            "note": (
                "Simulated workload: writes clearly labelled synthetic samples (source=simulated) so the "
                "baseline -> plan -> approval flow works without a Ditto. Baselines are stamped as simulated."
                if simulated else
                "Self-hosted fleet workload writes live SLI samples to the shared metrics dir. It needs a reachable Ditto; without one it reports an error and retries. It never injects a fault."
            ),
        }

    @application.get("/healthz")
    def healthz() -> dict[str, Any]:
        # Liveness only: reports that this process is serving. It deliberately makes no claim about
        # Ditto, Prometheus or any other external system being reachable.
        return {"status": "ok"}

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

    @application.get("/api/v1/runs/{run_id}/interaction")
    def run_interaction(run_id: str) -> dict[str, Any]:
        # Read-only: recomputed from the stored runs, so a verdict saved before a solo run existed is never shown stale.
        orchestrator = application.state.orchestrator
        run = orchestrator.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run was not found.")
        return views.run_interaction(run, orchestrator.list_runs(200))

    @application.get("/api/v1/remediations")
    def remediations() -> dict[str, Any]:
        # Read-only: fix registry, latest before/after verdict, and the deployment stamp of the last run.
        orchestrator = application.state.orchestrator
        return views.remediation_views(REPOSITORY_ROOT, orchestrator.settings.metrics_dir, orchestrator.list_runs(200))

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

    def graph_engine() -> AgentGraph:
        if getattr(application.state, "agent_graph", None) is None:
            application.state.agent_graph = AgentGraph(application.state.orchestrator)
        return application.state.agent_graph

    @application.get("/api/v1/agent-graph")
    def agent_graph() -> dict[str, Any]:
        # Read-only: graph structure and the last planning state. Never runs a node.
        return graph_engine().describe()

    # Advisory only (same effect as POST /baselines + /experiments/plan): the graph pauses
    # before approval and never executes. Execution stays in the operator CLI.
    @application.post("/api/v1/agent-graph/run", status_code=201)
    def run_agent_graph(request: AgentGraphRunRequest) -> dict[str, Any]:
        return graph_engine().run_planning(request.max_candidates, request.baseline_id)

    @application.post("/api/v1/agent-graph/resume")
    def resume_agent_graph() -> dict[str, Any]:
        try:
            return graph_engine().resume_after_approval()
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

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

    @application.get("/lab")
    def lab() -> FileResponse:
        return FileResponse(REPOSITORY_ROOT / "frontend" / "lab.html")

    application.mount("/assets", StaticFiles(directory=REPOSITORY_ROOT / "frontend"), name="assets")
    # Root-level fallback so the dashboard's own files (app.js, config.js, styles.css, lab.css,
    # lab.js) resolve at the site root on this API too, exactly as they do on the static host.
    # Registered last: every API route above matches before this catch-all.
    application.mount("/", StaticFiles(directory=REPOSITORY_ROOT / "frontend"), name="frontend")
    return application


app = create_app()