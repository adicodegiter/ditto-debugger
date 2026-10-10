import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from jsonschema import Draft202012Validator  # noqa: E402

from ai import dependency_map  # noqa: E402
from app.agents.discovery import DittoDiscoveryAgent, interpolate  # noqa: E402
from app.settings import Settings  # noqa: E402
from tools.export_topology import DEFAULT_ENV_FILE, export_topology  # noqa: E402


def _export(directory: Path) -> dict:
    _, graph = export_topology(
        ROOT / "deploy" / "ditto" / "docker-compose.yml",
        directory / "inventory.json",
        directory / "dependency_map.json",
        ROOT / "configs" / "experiment-policy.yaml",
        (DEFAULT_ENV_FILE,),
    )
    return graph


class DependencyMapTests(unittest.TestCase):
    def test_committed_map_and_page_are_up_to_date(self):
        self.assertEqual(dependency_map.main(["--check"]), 0, "run: python -m ai.dependency_map")

    def test_export_with_workbench_layer_is_schema_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = _export(Path(directory))
        graph_schema = json.loads((ROOT / "schemas" / "graph.schema.json").read_text(encoding="utf-8"))
        service_schema = json.loads((ROOT / "schemas" / "service.schema.json").read_text(encoding="utf-8"))
        graph_schema["properties"]["nodes"]["items"] = {"allOf": [service_schema, {"required": ["affected_slis"]}]}
        Draft202012Validator(graph_schema).validate(graph)

    def test_provenance_and_manual_labelling(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = _export(Path(directory))
        ditto = {n["id"] for n in graph["nodes"] if n["source"] == "compose"}
        manual = {n["id"] for n in graph["nodes"] if n.get("labels", {}).get("provenance_type") == "manual"}
        self.assertEqual(ditto, {"connectivity", "ditto-ui", "gateway", "mongodb", "nginx", "policies", "things", "things-search"})
        self.assertEqual(manual, {"fleet-workload", "workbench-api", "operator-cli", "watchdog", "data-metrics"})
        for edge in graph["edges"]:
            self.assertTrue(edge["provenance"])
            if edge["source"] in manual or edge["target"] in manual:
                self.assertEqual(edge["provenance_type"], "manual", edge)
            self.assertFalse(edge["runtime_observed"], "no edge is runtime-observed yet")
        slis = {n["id"]: n.get("labels", {}).get("workbench_slis") for n in graph["nodes"]}
        self.assertEqual(slis["things-search"], "search")
        self.assertEqual(slis["things"], "freshness,write")

    def test_no_manifest_means_no_map(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises((ValueError, OSError)):
                export_topology(root / "missing.yml", root / "i.json", root / "g.json", ROOT / "configs" / "experiment-policy.yaml")
            self.assertFalse((root / "g.json").exists())

    def test_compose_variables_resolve_from_env_file_then_defaults(self):
        self.assertEqual(interpolate("nginx:${V:-1.0}", {}), ("nginx:1.0", []))
        self.assertEqual(interpolate("nginx:${V:-1.0}", {"V": "2.0"}), ("nginx:2.0", []))
        self.assertEqual(interpolate("x:${V-1.0}", {"V": ""}), ("x:", []))
        self.assertEqual(interpolate("x:$V", {"V": "3"}), ("x:3", []))
        self.assertEqual(interpolate("x:${MISSING}", {}), ("x:${MISSING}", ["MISSING"]))
        settings = Settings.from_environment()
        report = DittoDiscoveryAgent(settings.compose_file, env_files=(settings.image_pins_file,)).discover().to_dict()
        components = {c["name"]: c for c in report["components"]}
        self.assertEqual(components["things-search"]["image"], "docker.io/eclipse/ditto-things-search:3.9.8")
        self.assertEqual(components["things-search"]["labels"]["image_declared"], "docker.io/eclipse/ditto-things-search:${DITTO_VERSION:-3.9.8}")
        self.assertIn((8080, 80), [(p["host_port"], p["container_port"]) for p in components["nginx"]["ports"]])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["status"], "available")


if __name__ == "__main__":
    unittest.main()
