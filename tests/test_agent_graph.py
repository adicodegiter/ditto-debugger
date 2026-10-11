from __future__ import annotations

import ast
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app import graph as agent_graph
from app.graph import END, AgentGraph, route_after_discover, route_after_measure
from app.main import create_app
from app.settings import Settings


def _baseline(baseline_id="baseline-1", graph_version="g1", coverage=1.0, fresh=True):
    return {"baseline_id": baseline_id, "graph_version": graph_version, "telemetry_status": "available",
            "telemetry": {"status": "available", "coverage": coverage}, "fresh": fresh}


class FakeOrchestrator:
    """Duck-typed stand-in: records calls so tests can assert which agent methods ran."""

    def __init__(self, graph_status="available", captures=(), stored=None):
        self.graph_status = graph_status
        self.captures = list(captures)
        self.calls: list[str] = []
        self.approval_status = "pending"
        stored = stored or {}
        self.repository = mock.Mock(get_baseline=lambda baseline_id: stored.get(baseline_id),
                                    list_baselines=lambda limit: list(stored.values())[:limit])
        self.planning_agent = mock.Mock(_baseline_is_fresh=lambda baseline: baseline.get("fresh", False))

    def discover_architecture(self):
        self.calls.append("discover")
        return {"status": self.graph_status, "graph_version": "g1"}

    def capture_baseline(self):
        self.calls.append("capture")
        item = self.captures.pop(0) if self.captures else ValueError("Baseline refused: no samples")
        if isinstance(item, Exception):
            raise item
        return item

    def plan_experiments(self, baseline_id, max_candidates):
        self.calls.append(f"plan:{baseline_id}")
        return {"plan_id": "plan-1", "candidates": [{"id": "restart-things-search-1", "target_component": "things-search",
                                                      "action": "restart_container", "approval_status": "pending"}]}

    def get_experiment(self, experiment_id):
        return {"id": experiment_id, "approval_status": self.approval_status}

    def diagnose_run(self, run, candidate, baseline):
        self.calls.append("diagnose")
        return {"comparison": {"write": {"verdict": "as_predicted"}}, "prediction_accuracy": 1.0,
                "recovery_status": "recovered", "summary": "ok", "evidence_use": "experiment", "kill_switch": {}}


def engines():
    yield True  # sequential fallback always runs
    if agent_graph.LANGGRAPH_AVAILABLE:
        yield False


class GateRoutingTests(unittest.TestCase):
    def test_route_functions(self):
        self.assertEqual(route_after_discover({"graph_status": "available"}), "measure")
        self.assertEqual(route_after_discover({"graph_status": "unavailable"}), END)
        self.assertEqual(route_after_measure({"gate": "pass"}), "plan")
        self.assertEqual(route_after_measure({"gate": "baseline_rejected", "measure_attempts": 1, "max_measure_attempts": 2}), "measure")
        self.assertEqual(route_after_measure({"gate": "baseline_rejected", "measure_attempts": 2, "max_measure_attempts": 2}), END)

    def test_no_graph_ends_before_measuring(self):
        for fallback in engines():
            with self.subTest(fallback=fallback):
                orchestrator = FakeOrchestrator(graph_status="unavailable")
                state = AgentGraph(orchestrator, force_fallback=fallback).run_planning()
                self.assertEqual(state["gate"], "no_graph")
                self.assertEqual(state["trace"], ["discover"])
                self.assertEqual(state["interrupted_before"], [])
                self.assertEqual(orchestrator.calls, ["discover"])

    def test_stale_baseline_routes_back_to_measure_then_plans(self):
        for fallback in engines():
            with self.subTest(fallback=fallback):
                orchestrator = FakeOrchestrator(captures=[_baseline("b-stale", fresh=False), _baseline("b-fresh")])
                state = AgentGraph(orchestrator, force_fallback=fallback).run_planning()
                self.assertEqual(state["trace"], ["discover", "measure", "measure", "plan"])
                self.assertIn("plan:b-fresh", orchestrator.calls)
                self.assertEqual(state["interrupted_before"], ["approval"])

    def test_low_coverage_ends_with_reason_after_retries(self):
        for fallback in engines():
            with self.subTest(fallback=fallback):
                orchestrator = FakeOrchestrator(captures=[_baseline(coverage=0.5), _baseline(coverage=0.5)])
                state = AgentGraph(orchestrator, force_fallback=fallback).run_planning()
                self.assertEqual(state["trace"], ["discover", "measure", "measure"])
                self.assertEqual(state["gate"], "baseline_rejected")
                self.assertIn("coverage", state["reason"])
                self.assertFalse(any(call.startswith("plan") for call in orchestrator.calls))

    def test_refused_capture_ends_with_reason(self):
        for fallback in engines():
            with self.subTest(fallback=fallback):
                orchestrator = FakeOrchestrator(captures=[])
                state = AgentGraph(orchestrator, force_fallback=fallback).run_planning(max_measure_attempts=1)
                self.assertEqual(state["gate"], "baseline_rejected")
                self.assertIn("no samples", state["reason"])

    def test_supplied_fresh_baseline_is_reused_without_capture(self):
        for fallback in engines():
            with self.subTest(fallback=fallback):
                orchestrator = FakeOrchestrator(stored={"b-1": _baseline("b-1")})
                state = AgentGraph(orchestrator, force_fallback=fallback).run_planning(baseline_id="b-1")
                self.assertNotIn("capture", orchestrator.calls)
                self.assertEqual(state["baseline_id"], "b-1")

    def test_latest_stored_baseline_is_reused_when_fresh(self):
        for fallback in engines():
            with self.subTest(fallback=fallback):
                orchestrator = FakeOrchestrator(stored={"b-latest": _baseline("b-latest")})
                state = AgentGraph(orchestrator, force_fallback=fallback).run_planning()
                self.assertNotIn("capture", orchestrator.calls)
                self.assertEqual(state["baseline_id"], "b-latest")

    def test_stale_stored_baseline_triggers_capture(self):
        for fallback in engines():
            with self.subTest(fallback=fallback):
                orchestrator = FakeOrchestrator(stored={"b-old": _baseline("b-old", fresh=False)}, captures=[_baseline("b-new")])
                state = AgentGraph(orchestrator, force_fallback=fallback).run_planning()
                self.assertIn("capture", orchestrator.calls)
                self.assertEqual(state["baseline_id"], "b-new")

    def test_mismatched_graph_version_is_rejected(self):
        for fallback in engines():
            with self.subTest(fallback=fallback):
                orchestrator = FakeOrchestrator(captures=[_baseline(graph_version="old")])
                state = AgentGraph(orchestrator, force_fallback=fallback).run_planning(max_measure_attempts=1)
                self.assertIn("graph version", state["reason"])


class InterruptTests(unittest.TestCase):
    def test_pauses_before_approval_and_resume_only_reports(self):
        for fallback in engines():
            with self.subTest(fallback=fallback):
                orchestrator = FakeOrchestrator(captures=[_baseline()])
                graph = AgentGraph(orchestrator, force_fallback=fallback)
                paused = graph.run_planning()
                self.assertEqual(paused["interrupted_before"], ["approval"])
                self.assertEqual(paused["gate"], "awaiting_approval")
                self.assertNotIn("approval", paused["trace"])
                orchestrator.approval_status = "approved"
                done = graph.resume_after_approval()
                self.assertEqual(done["trace"][-1], "approval")
                self.assertEqual(done["gate"], "handed_to_operator_cli")
                self.assertIn("safety.cli execute", done["approvals"][0]["next_step"])
                with self.assertRaises(ValueError):
                    graph.resume_after_approval()

    @unittest.skipUnless(agent_graph.LANGGRAPH_AVAILABLE, "langgraph not installed")
    def test_langgraph_compiled_with_interrupt(self):
        graph = AgentGraph(FakeOrchestrator(captures=[_baseline()]))
        self.assertEqual(graph.engine, "langgraph")
        graph.run_planning()
        config = {"configurable": {"thread_id": graph.last_thread_id}}
        self.assertEqual(graph._planning.get_state(config).next, ("approval",))


class FallbackTests(unittest.TestCase):
    def test_fallback_when_langgraph_missing(self):
        with mock.patch.object(agent_graph, "LANGGRAPH_AVAILABLE", False):
            graph = AgentGraph(FakeOrchestrator(captures=[_baseline()]))
            self.assertEqual(graph.engine, "sequential-fallback")
            state = graph.run_planning()
        self.assertEqual(state["engine"], "sequential-fallback")
        self.assertEqual(state["interrupted_before"], ["approval"])

    def test_analysis_graph_is_deterministic_without_api_key(self):
        run = {"run_id": "run-1", "observations": []}
        for fallback in engines():
            with self.subTest(fallback=fallback), mock.patch.dict("os.environ", {"AI_PROVIDER": "", "GROQ_API_KEY": ""}):
                orchestrator = FakeOrchestrator()
                result = AgentGraph(orchestrator, force_fallback=fallback).analyse_run(run, {"id": "x"}, None)
                self.assertEqual(result["trace"], ["diagnose", "compare", "report"])
                self.assertEqual(result["predicted_vs_observed"]["prediction_accuracy"], 1.0)
                self.assertIsInstance(result["findings"], list)

    def test_real_orchestrator_without_manifest_ends_at_discover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root / "missing.yml", (), "http://127.0.0.1:9090", root / "db.sqlite3",
                                ROOT / "configs" / "experiment-policy.yaml")
            from app.orchestrator import AgentOrchestrator
            state = AgentGraph(AgentOrchestrator(settings)).run_planning()
        self.assertEqual(state["gate"], "no_graph")


class NoExecutionTests(unittest.TestCase):
    FORBIDDEN_MODULES = ("safety.runner", "safety.cli", "safety.docker_adapter", "safety.kill_switch",
                         "safety.watchdog", "safety.recovery", "subprocess", "docker")
    FORBIDDEN_NAMES = {"execute", "execute_experiment", "prepare_execution", "record_operator_run", "record_approval",
                       "ExperimentRunner", "RunnerError", "DockerAdapter", "subprocess", "Popen", "system"}

    def test_graph_module_imports_nothing_that_executes(self):
        tree = ast.parse(Path(agent_graph.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(("." * node.level) + (node.module or ""))
        for module in imported:
            for forbidden in self.FORBIDDEN_MODULES:
                self.assertFalse(module == forbidden or module.startswith(forbidden + "."), module)
            self.assertNotIn("runner", module)

    def test_graph_source_never_references_execution_paths(self):
        tree = ast.parse(Path(agent_graph.__file__).read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertEqual(names & self.FORBIDDEN_NAMES, set())

    def test_node_callables_cannot_reach_the_runner(self):
        graph = AgentGraph(FakeOrchestrator(), force_fallback=True)
        nodes = {**graph.planning_nodes, **graph.analysis_nodes}

        def code_names(code):
            names = set(code.co_names)
            for const in code.co_consts:
                if inspect.iscode(const):
                    names |= code_names(const)
            return names

        for name, function in nodes.items():
            with self.subTest(node=name):
                self.assertEqual(code_names(function.__code__) & self.FORBIDDEN_NAMES, set())
                self.assertNotIn("safety", function.__globals__)
        self.assertNotIn("execute", set(agent_graph.PLANNING_NODES) | set(agent_graph.ANALYSIS_NODES))


class AgentGraphApiTests(unittest.TestCase):
    def test_endpoint_is_read_only_and_execute_stays_503(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root / "missing.yml", (), "http://127.0.0.1:9090", root / "db.sqlite3",
                                ROOT / "configs" / "experiment-policy.yaml")
            client = TestClient(create_app(settings))
            body = client.get("/api/v1/agent-graph").json()
            self.assertEqual(client.post("/api/v1/agent-graph").status_code, 405)
            self.assertEqual(client.post("/api/v1/experiments/anything/execute").status_code, 503)
        self.assertEqual(body["planning"]["interrupt_before"], ["approval"])
        self.assertEqual(body["planning"]["nodes"], ["discover", "measure", "plan", "approval"])
        self.assertFalse(body["planning"]["executes"])
        self.assertIsNone(body["last_planning_state"])
        self.assertIn(body["engine"], {"langgraph", "sequential-fallback"})

    def test_run_and_resume_endpoints_are_advisory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root / "missing.yml", (), "http://127.0.0.1:9090", root / "db.sqlite3",
                                ROOT / "configs" / "experiment-policy.yaml")
            client = TestClient(create_app(settings))
            self.assertEqual(client.post("/api/v1/agent-graph/resume").status_code, 409)  # nothing paused
            run = client.post("/api/v1/agent-graph/run", json={"max_candidates": 2})
            self.assertEqual(run.status_code, 201)
            self.assertEqual(run.json()["gate"], "no_graph")
            self.assertEqual(client.get("/api/v1/agent-graph").json()["last_planning_state"]["trace"], ["discover"])
            self.assertEqual(client.post("/api/v1/agent-graph/run", json={"max_candidates": 99}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
