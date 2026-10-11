"""Optional LangGraph orchestration of the three advisory agents.

The nodes WRAP existing AgentOrchestrator methods; they do not reimplement them:

    planning:  discover (Agent 1) -> measure (Agent 2) -> plan (Agent 3) -> [interrupt] approval -> END
    analysis:  diagnose (Agent 3) -> compare (predicted vs observed) -> report (findings) -> END

Gates are conditional edges, the same checks the planner enforces: no graph ends the run,
a missing / stale / low-coverage baseline routes back to measure (bounded retries) or ends
with a reason. The planning graph is compiled with interrupt_before=["approval"] and ENDS at
approval: approving is a typed human decision (API / operator CLI), and executing is only
ever done by the operator CLI (python -m safety.cli execute). No node in this module may
import or call the runner, the Docker adapter or any execute path
(tests/test_agent_graph.py enforces this).

LangGraph is optional. Without it, the same node functions run sequentially with the same
routing (`engine = "sequential-fallback"`).
"""
from __future__ import annotations

import argparse
import json
import uuid
from typing import Any, Callable, TypedDict

try:  # optional dependency (requirements-graph.txt)
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the fallback tests via force_fallback
    LANGGRAPH_AVAILABLE = False
    END, START = "__end__", "__start__"

from ai.explain import explain_findings
from ai.report import generate_findings

MIN_COVERAGE = 0.8  # same floor as ExperimentPlanningAgent.plan
DEFAULT_MEASURE_ATTEMPTS = 2

PLANNING_NODES = ("discover", "measure", "plan", "approval")
ANALYSIS_NODES = ("diagnose", "compare", "report")
PLANNING_EDGES = (
    (START, "discover"),
    ("discover", "measure", "graph available"),
    ("discover", END, "no graph"),
    ("measure", "plan", "baseline fresh, coverage >= 80%, matches graph"),
    ("measure", "measure", "baseline missing / stale / low coverage (bounded retry)"),
    ("measure", END, "gate failed after retries"),
    ("plan", "approval", "interrupt: human approval"),
    ("approval", END),
)
ANALYSIS_EDGES = ((START, "diagnose"), ("diagnose", "compare"), ("compare", "report"), ("report", END))


class PlanningState(TypedDict, total=False):
    max_candidates: int
    max_measure_attempts: int
    baseline_id: str
    graph_version: str
    graph_status: str
    measure_attempts: int
    gate: str
    reason: str
    plan_id: str
    candidates: list[dict[str, Any]]
    approvals: list[dict[str, Any]]
    trace: list[str]


class AnalysisState(TypedDict, total=False):
    run: dict[str, Any]
    candidate: dict[str, Any]
    baseline: dict[str, Any] | None
    diagnosis: dict[str, Any]
    predicted_vs_observed: dict[str, Any]
    findings: list[dict[str, Any]]
    trace: list[str]


def _trace(state: dict[str, Any], node: str) -> list[str]:
    return [*state.get("trace", []), node]


# ---- gates (pure functions over state; shared by both engines) ----

def baseline_gate(orchestrator: Any, baseline: dict[str, Any] | None, graph_version: str | None) -> tuple[bool, str]:
    """Return (ok, reason). Mirrors the planner's own refusal rules so routing never disagrees with it."""
    if baseline is None:
        return False, "no baseline"
    telemetry = baseline.get("telemetry") or {}
    if baseline.get("telemetry_status") != "available" or telemetry.get("status") != "available":
        return False, "baseline telemetry is not healthy"
    if float(telemetry.get("coverage", 0) or 0) < MIN_COVERAGE:
        return False, f"telemetry coverage below {MIN_COVERAGE:.0%}"
    if baseline.get("graph_version") != graph_version:
        return False, "baseline does not match the current graph version"
    if not orchestrator.planning_agent._baseline_is_fresh(baseline):
        return False, "baseline is stale"
    return True, "ok"


def route_after_discover(state: PlanningState) -> str:
    return "measure" if state.get("graph_status") == "available" else END


def route_after_measure(state: PlanningState) -> str:
    if state.get("gate") == "pass":
        return "plan"
    if state.get("measure_attempts", 0) < state.get("max_measure_attempts", DEFAULT_MEASURE_ATTEMPTS):
        return "measure"
    return END


# ---- planning nodes (wrap AgentOrchestrator; advisory only) ----

def make_planning_nodes(orchestrator: Any) -> dict[str, Callable[[PlanningState], dict[str, Any]]]:
    def discover(state: PlanningState) -> dict[str, Any]:
        """Agent 1: versioned dependency graph from the manifests."""
        try:
            graph = orchestrator.discover_architecture()
        except (OSError, ValueError) as error:
            return {"graph_status": "unavailable", "gate": "no_graph", "reason": str(error), "trace": _trace(state, "discover")}
        update: dict[str, Any] = {"graph_status": graph.get("status"), "graph_version": graph.get("graph_version"), "trace": _trace(state, "discover")}
        if graph.get("status") != "available":
            update.update(gate="no_graph", reason="architecture discovery is unavailable")
        return update

    def measure(state: PlanningState) -> dict[str, Any]:
        """Agent 2: reuse a supplied fresh baseline, otherwise capture one from data/metrics."""
        attempts = state.get("measure_attempts", 0) + 1
        baseline = None
        reason = ""
        if state.get("baseline_id") and attempts == 1:
            baseline = orchestrator.repository.get_baseline(state["baseline_id"])
            reason = "" if baseline else "supplied baseline was not found"
        if baseline is None or not baseline_gate(orchestrator, baseline, state.get("graph_version"))[0]:
            try:
                baseline = orchestrator.capture_baseline()
            except ValueError as error:
                baseline, reason = None, str(error)
        ok, gate_reason = baseline_gate(orchestrator, baseline, state.get("graph_version"))
        update: dict[str, Any] = {"measure_attempts": attempts, "gate": "pass" if ok else "baseline_rejected",
                                  "reason": "ok" if ok else (reason or gate_reason), "trace": _trace(state, "measure")}
        if baseline is not None:
            update["baseline_id"] = baseline["baseline_id"]
        return update

    def plan(state: PlanningState) -> dict[str, Any]:
        """Agent 3: ranked, policy-checked candidates. Deterministic; nothing executes."""
        try:
            stored = orchestrator.plan_experiments(state["baseline_id"], state.get("max_candidates", 3))
        except (LookupError, ValueError) as error:
            return {"gate": "plan_refused", "reason": str(error), "candidates": [], "trace": _trace(state, "plan")}
        candidates = [
            {key: item.get(key) for key in ("id", "target_component", "action", "duration_seconds", "predicted_slis",
                                    "risk_score", "selection_score", "reason", "candidate_hash", "approval_status")}
            for item in stored.get("candidates", [])
        ]
        return {"plan_id": stored.get("plan_id"), "candidates": candidates, "gate": "awaiting_approval",
                "reason": "a human must approve one candidate", "trace": _trace(state, "plan")}

    def approval(state: PlanningState) -> dict[str, Any]:
        """Reached only after a human resumes the graph. Reads approval status; never executes."""
        approvals = []
        for candidate in state.get("candidates", []):
            stored = orchestrator.get_experiment(candidate["id"]) or {}
            status = stored.get("approval_status", "unknown")
            approvals.append({
                "experiment_id": candidate["id"],
                "approval_status": status,
                "next_step": f"python -m safety.cli execute {candidate['id']} --dry-run" if status == "approved" else "record a typed approval first",
            })
        return {"approvals": approvals, "gate": "handed_to_operator_cli",
                "reason": "execution happens only in the operator CLI", "trace": _trace(state, "approval")}

    return {"discover": discover, "measure": measure, "plan": plan, "approval": approval}


# ---- analysis nodes (post-run; Agent 3) ----

def make_analysis_nodes(orchestrator: Any) -> dict[str, Callable[[AnalysisState], dict[str, Any]]]:
    def diagnose(state: AnalysisState) -> dict[str, Any]:
        diagnosis = orchestrator.diagnose_run(state["run"], state["candidate"], state.get("baseline"))
        return {"diagnosis": diagnosis, "trace": _trace(state, "diagnose")}

    def compare(state: AnalysisState) -> dict[str, Any]:
        diagnosis = state["diagnosis"]
        return {"predicted_vs_observed": {
            "comparison": diagnosis.get("comparison", {}),
            "prediction_accuracy": diagnosis.get("prediction_accuracy"),
            "recovery_status": diagnosis.get("recovery_status"),
            "summary": diagnosis.get("summary"),
        }, "trace": _trace(state, "compare")}

    def report(state: AnalysisState) -> dict[str, Any]:
        """Deterministic findings; optional Groq rewording (AI_PROVIDER=groq) is schema-checked in ai.explain."""
        run = {**state["run"], "diagnosis": state["diagnosis"]}
        findings = explain_findings(generate_findings(run), run)
        return {"findings": findings, "trace": _trace(state, "report")}

    return {"diagnose": diagnose, "compare": compare, "report": report}


# ---- engines ----

class AgentGraph:
    """Builds the LangGraph graphs when available; otherwise runs the same nodes sequentially."""

    def __init__(self, orchestrator: Any, force_fallback: bool = False) -> None:
        self.orchestrator = orchestrator
        self.use_langgraph = LANGGRAPH_AVAILABLE and not force_fallback
        self.planning_nodes = make_planning_nodes(orchestrator)
        self.analysis_nodes = make_analysis_nodes(orchestrator)
        self.last_planning_state: dict[str, Any] | None = None
        self.last_thread_id: str | None = None
        self._planning = self._analysis = None
        if self.use_langgraph:
            self._planning = self._build_planning()
            self._analysis = self._build_analysis()

    @property
    def engine(self) -> str:
        return "langgraph" if self.use_langgraph else "sequential-fallback"

    def _build_planning(self) -> Any:
        builder = StateGraph(PlanningState)
        for name in PLANNING_NODES:
            builder.add_node(name, self.planning_nodes[name])
        builder.add_edge(START, "discover")
        builder.add_conditional_edges("discover", route_after_discover, {"measure": "measure", END: END})
        builder.add_conditional_edges("measure", route_after_measure, {"plan": "plan", "measure": "measure", END: END})
        builder.add_edge("plan", "approval")
        builder.add_edge("approval", END)
        return builder.compile(checkpointer=InMemorySaver(), interrupt_before=["approval"])

    def _build_analysis(self) -> Any:
        builder = StateGraph(AnalysisState)
        for name in ANALYSIS_NODES:
            builder.add_node(name, self.analysis_nodes[name])
        builder.add_edge(START, "diagnose")
        builder.add_edge("diagnose", "compare")
        builder.add_edge("compare", "report")
        builder.add_edge("report", END)
        return builder.compile()

    def run_planning(self, max_candidates: int = 3, baseline_id: str | None = None,
                     max_measure_attempts: int = DEFAULT_MEASURE_ATTEMPTS) -> dict[str, Any]:
        """Run discover -> measure -> plan and stop before approval (or end early at a gate)."""
        initial: PlanningState = {"max_candidates": max_candidates, "max_measure_attempts": max_measure_attempts,
                                  "measure_attempts": 0, "trace": []}
        if baseline_id:
            initial["baseline_id"] = baseline_id
        self.last_thread_id = f"plan-{uuid.uuid4().hex[:12]}"
        if self.use_langgraph:
            config = {"configurable": {"thread_id": self.last_thread_id}}
            state = dict(self._planning.invoke(initial, config))
            pending = list(self._planning.get_state(config).next)
        else:
            state, pending = self._run_sequential(initial)
        state.update(thread_id=self.last_thread_id, engine=self.engine, interrupted_before=pending)
        self.last_planning_state = state
        return state

    def resume_after_approval(self, thread_id: str | None = None) -> dict[str, Any]:
        """Resume a paused planning run at the approval node (reports status; executes nothing)."""
        thread_id = thread_id or self.last_thread_id
        if not self.last_planning_state or self.last_planning_state.get("interrupted_before") != ["approval"]:
            raise ValueError("No planning run is paused at approval.")
        if self.use_langgraph:
            state = dict(self._planning.invoke(None, {"configurable": {"thread_id": thread_id}}))
        else:
            state = {**self.last_planning_state, **self.planning_nodes["approval"](self.last_planning_state)}
        state.update(thread_id=thread_id, engine=self.engine, interrupted_before=[])
        self.last_planning_state = state
        return state

    def _run_sequential(self, state: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        routes = {"discover": route_after_discover, "measure": route_after_measure, "plan": lambda _: "approval"}
        node = "discover"
        while node != END:
            if node == "approval":
                return state, ["approval"]  # same pause point as interrupt_before
            state = {**state, **self.planning_nodes[node](state)}
            node = routes[node](state)
        return state, []

    def analyse_run(self, run: dict[str, Any], candidate: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any]:
        """Post-run analysis: diagnose -> predicted vs observed -> findings. Does not persist anything."""
        state: dict[str, Any] = {"run": run, "candidate": candidate, "baseline": baseline, "trace": []}
        if self.use_langgraph:
            result = dict(self._analysis.invoke(state))
        else:
            result = state
            for name in ANALYSIS_NODES:
                result = {**result, **self.analysis_nodes[name](result)}
        result["engine"] = self.engine
        return result

    def describe(self) -> dict[str, Any]:
        """Read-only structure + last state for the dashboard. Never runs a node."""
        def edges(items: tuple) -> list[dict[str, str]]:
            return [{"source": item[0], "target": item[1], **({"condition": item[2]} if len(item) > 2 else {})} for item in items]
        return {
            "engine": self.engine,
            "langgraph_available": LANGGRAPH_AVAILABLE,
            "planning": {"nodes": list(PLANNING_NODES), "edges": edges(PLANNING_EDGES), "interrupt_before": ["approval"],
                         "ends_at": "approval", "executes": False},
            "analysis": {"nodes": list(ANALYSIS_NODES), "edges": edges(ANALYSIS_EDGES), "executes": False},
            "execution": "operator CLI only: python -m safety.cli execute <experiment_id> (typed approval required)",
            "last_planning_state": self.last_planning_state,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the advisory agent graph (discover -> measure -> plan) and stop at human approval. Never executes faults.")
    parser.add_argument("--max-candidates", type=int, default=3)
    parser.add_argument("--baseline-id", default=None, help="reuse a stored baseline if it passes the gates")
    parser.add_argument("--fallback", action="store_true", help="force the sequential engine even if langgraph is installed")
    parser.add_argument("--describe", action="store_true", help="print the graph structure only")
    args = parser.parse_args(argv)

    from .orchestrator import AgentOrchestrator
    from .settings import Settings

    graph = AgentGraph(AgentOrchestrator(Settings.from_environment()), force_fallback=args.fallback)
    result = graph.describe() if args.describe else graph.run_planning(args.max_candidates, args.baseline_id)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
