import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from app.agents.discovery import DittoDiscoveryAgent, interpolate  # noqa: E402
from app.settings import Settings  # noqa: E402
from ai import dependency_map  # noqa: E402


class DependencyMapTests(unittest.TestCase):
    def test_committed_map_is_up_to_date(self):
        self.assertEqual(dependency_map.main(["--check"]), 0, "run: python -m ai.dependency_map")

    def test_every_edge_has_provenance_and_workbench_parts_are_manual(self):
        built = dependency_map.build(Settings.from_environment())
        ditto_ids = {node["id"] for node in built["nodes"] if node["provenance_type"] == "configured"}
        self.assertEqual(ditto_ids, {"connectivity", "ditto-ui", "gateway", "mongodb", "nginx", "policies", "things", "things-search"})
        for edge in built["edges"]:
            self.assertIn(edge["provenance_type"], {"configured", "documented", "manual"})
            self.assertTrue(edge["provenance"])
            if edge["source"] not in ditto_ids or edge["target"] not in ditto_ids:
                self.assertEqual(edge["provenance_type"], "manual", edge)
        slis = {node["id"]: node["slis"] for node in built["nodes"]}
        self.assertEqual(slis["things-search"], ["search"])
        self.assertIn("write", slis["things"])
        self.assertNotIn("search", slis["things"])

    def test_no_manifest_means_no_map(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root / "missing.yml", (), "http://127.0.0.1:9090", root / "db.sqlite3", ROOT / "configs" / "experiment-policy.yaml")
            with self.assertRaises(SystemExit):
                dependency_map.build(settings)

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


if __name__ == "__main__":
    unittest.main()
