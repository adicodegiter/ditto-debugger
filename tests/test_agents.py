from __future__ import annotations

import sys
import tempfile
import time
import unittest
import json
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agents.discovery import DittoDiscoveryAgent
from app.agents.mapping import ArchitectureMapperAgent
from app.agents.planning import ExperimentPlanningAgent
from app.agents.telemetry import TelemetryObservabilityAgent, normalize_prometheus_result
from app.settings import Settings
from app.store import LocalRepository


class DiscoveryAgentTests(unittest.TestCase):
    def test_missing_manifest_returns_empty_unavailable_graph_not_a_demo_topology(self):
        with tempfile.TemporaryDirectory() as directory:
            report = DittoDiscoveryAgent(Path(directory) / "missing-compose.yml").discover()
        graph = ArchitectureMapperAgent().map(report)
        self.assertEqual(report.status, "unavailable")
        self.assertEqual(graph["nodes"], [])
        self.assertEqual(graph["edges"], [])
        self.assertEqual(graph["clusters"], [])
        self.assertFalse(any(item.runtime_observed for item in report.relationships))

    def test_compose_records_only_declared_static_edges_and_internal_ports(self):
        manifest = """
services:
  mongodb:
    image: mongo:7.0
  gateway:
    image: eclipse/ditto-gateway:3.9.8
    depends_on:
      policies:
        condition: service_started
    environment:
      MONGO_DB_HOSTNAME: mongodb
      TOKEN: should-not-be-exported
    ports:
      - 127.0.0.1:8081:8080
    healthcheck:
      test: [CMD-SHELL, "curl --fail `hostname`:7626/alive || exit 1"]
    networks: [ditto]
  policies:
    image: eclipse/ditto-policies:3.9.8
networks:
  ditto: {}
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compose.yml"
            path.write_text(manifest, encoding="utf-8")
            report = DittoDiscoveryAgent(path).discover()
        graph = ArchitectureMapperAgent().map(report)
        gateway = next(item for item in graph["nodes"] if item["id"] == "gateway")
        self.assertEqual(report.status, "available")
        self.assertEqual(gateway["source"], "compose")
        self.assertEqual(gateway["health_url"], "http://gateway:7626/alive")
        self.assertEqual(gateway["metrics_url"], "http://gateway:9095/")
        self.assertEqual(gateway["ports"][0]["host_ip"], "127.0.0.1")
        self.assertEqual(gateway["affected_slis"], ["service_availability", "error_rate", "p95_latency"])
        self.assertNotIn("TOKEN", str(graph))
        configured_edges = [edge for edge in graph["edges"] if edge["evidence_type"] == "static"]
        documented_edges = [edge for edge in graph["edges"] if edge["evidence_type"] == "documented"]
        self.assertEqual({edge["relation"] for edge in configured_edges}, {"compose_depends_on", "configured_database_host"})
        self.assertEqual({edge["relation"] for edge in documented_edges}, {"pekko_cluster_communication", "documented_persistence"})
        self.assertTrue(all(edge["runtime_observed"] is False for edge in graph["edges"]))
        policy_impact = next(item for item in graph["impact_paths"]["policies"] if item["impacted_component"] == "gateway")
        self.assertEqual(policy_impact["path"], ["policies", "gateway"])
        self.assertEqual(graph["clusters"][0]["members"], ["gateway", "policies"])
        self.assertFalse(graph["clusters"][0]["complete"])

    def test_kubernetes_service_selector_links_to_manifest_workload(self):
        manifests = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: gateway
  namespace: ditto
spec:
  selector:
    matchLabels:
      release: ditto
  template:
    metadata:
      labels:
        release: ditto
    spec:
      containers:
        - name: gateway
          image: eclipse/ditto-gateway:3.9.8
          ports:
            - containerPort: 8080
          readinessProbe:
            httpGet:
              path: /alive
              port: 7626
---
apiVersion: v1
kind: Service
metadata:
  name: gateway
  namespace: ditto
spec:
  selector:
    release: ditto
  ports:
    - port: 8080
      targetPort: 8080
      nodePort: 30080
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ditto.yml"
            path.write_text(manifests, encoding="utf-8")
            report = DittoDiscoveryAgent(Path(directory) / "absent-compose.yml", [path]).discover()
        graph = ArchitectureMapperAgent().map(report)
        self.assertEqual(report.status, "available")
        self.assertEqual(len(graph["edges"]), 1)
        edge = graph["edges"][0]
        self.assertEqual(edge["relation"], "kubernetes_service_selector")
        self.assertFalse(edge["runtime_observed"])
        workload = next(item for item in graph["nodes"] if item["id"] == "ditto/gateway/gateway")
        self.assertEqual(workload["health_checks"][0]["http_path"], "/alive")
        self.assertEqual(workload["labels"]["ditto_role"], "Gateway")
        self.assertNotIn("release", workload["labels"])
        service = next(item for item in graph["nodes"] if item["id"] == "ditto/gateway")
        self.assertEqual(service["ports"][0]["target_port"], 8080)
        self.assertEqual(graph["clusters"][0]["members"], ["ditto/gateway/gateway"])


class TelemetryTests(unittest.TestCase):
    def test_prometheus_normalization_marks_staleness_and_drops_sensitive_labels(self):
        result = [{
            "metric": {"job": "ditto", "instance": "gateway:9095", "ditto_request_path": "/things/private-id"},
            "value": [100.0, "1"],
        }]
        observation = normalize_prometheus_result(result, "up", now_epoch=140.0, stale_after_seconds=30)[0]
        self.assertEqual(observation.quality, "stale")
        self.assertNotIn("ditto_request_path", observation.labels)

    def test_up_scrape_without_request_and_jvm_metrics_is_degraded(self):
        class PrometheusFixture(TelemetryObservabilityAgent):
            def _query(self, query):
                result = [{"metric": {"job": "ditto", "instance": "gateway:9095"}, "value": [time.time(), "1"]}] if query == "up" else []
                return {"status": "success", "data": {"result": result}}

        result = PrometheusFixture("http://127.0.0.1:9090").collect()
        self.assertEqual(result["status"], "degraded")
        self.assertLess(result["coverage"], 1.0)


class PlanningAgentTests(unittest.TestCase):
    def test_planner_requires_current_matching_baseline_and_ranks_deterministically(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = LocalRepository(Path(directory) / "test.sqlite3")
            repository.record_outcome("gateway", "restart_container", "no_failure_observed", "run:one")
            agent = ExperimentPlanningAgent(ROOT / "configs" / "experiment-policy.yaml", repository)
            captured = datetime.now(timezone.utc).isoformat()
            graph = {
                "status": "available",
                "graph_version": "graph-test",
                "nodes": [
                    {"id": "gateway", "kind": "ditto_service", "labels": {"ditto_role": "Gateway"}},
                    {"id": "things-search", "kind": "ditto_service", "labels": {"ditto_role": "Things-Search"}},
                    {"id": "mongodb", "kind": "database", "labels": {}},
                ],
                "criticality": {"gateway": {"configured_dependents": 0}, "things-search": {"configured_dependents": 3}},
            }
            telemetry = {
                "status": "available",
                "coverage": 1.0,
                "observations": [{"metric": "up", "quality": "fresh"}, {"metric": "jvm_threads", "quality": "fresh"}],
            }
            baseline = {
                "baseline_id": "baseline-test",
                "captured_at": captured,
                "graph_version": "graph-test",
                "telemetry_status": "available",
            }
            result = agent.plan(graph, telemetry, baseline)
            schema = json.loads((ROOT / "schemas" / "experiment.schema.json").read_text(encoding="utf-8"))
            self.assertEqual(len(result["candidates"]), 2)
            self.assertEqual(result["candidates"][0]["target_component"], "things-search")
            self.assertTrue(all(item["approval_required"] for item in result["candidates"]))
            self.assertTrue(all(item["action"] == "restart_container" for item in result["candidates"]))
            self.assertNotIn("mongodb", {item["target_component"] for item in result["candidates"]})
            self.assertFalse(result["execution_enabled"])
            for candidate in result["candidates"]:
              Draft202012Validator(schema).validate(candidate)

    def test_planner_refuses_stale_or_mismatched_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = LocalRepository(Path(directory) / "test.sqlite3")
            agent = ExperimentPlanningAgent(ROOT / "configs" / "experiment-policy.yaml", repository)
            with self.assertRaisesRegex(ValueError, "baseline"):
                agent.plan({"status": "available", "graph_version": "new", "nodes": [], "criticality": {}},
                           {"status": "available", "coverage": 1.0, "observations": []},
                           {"baseline_id": "old", "captured_at": datetime.now(timezone.utc).isoformat(), "graph_version": "old", "telemetry_status": "available"})


if __name__ == "__main__":
    unittest.main()