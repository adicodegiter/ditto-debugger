import json
import re
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]


class _ComposeTagLoader(yaml.SafeLoader):
    """SafeLoader that also accepts Docker Compose extension tags (e.g. ``!override``).

    ``deploy/ditto/docker-compose.hosted.yml`` uses Compose's ``!override`` merge tag, which plain
    ``yaml.safe_load`` refuses. The tag only changes merge semantics for Compose itself; for the
    parse check the tagged value is enough.
    """


def _unknown_tag_constructor(loader: _ComposeTagLoader, _tag_suffix: str, node: yaml.Node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_ComposeTagLoader.add_multi_constructor("!", _unknown_tag_constructor)


class ContractTests(unittest.TestCase):
    def test_examples_validate_against_matching_schemas(self):
        examples = {
            "service-gateway.json": "service.schema.json",
            "experiment-restart-gateway.json": "experiment.schema.json",
            "experiment-run.json": "experiment-run.schema.json",
            "finding.json": "finding.schema.json",
        }
        for example_name, schema_name in examples.items():
            with self.subTest(example=example_name):
                schema = json.loads((ROOT / "schemas" / schema_name).read_text(encoding="utf-8"))
                example = json.loads((ROOT / "schemas" / "examples" / example_name).read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(example)

    def test_yaml_configs_have_required_safety_and_metrics_fields(self):
        policy = yaml.safe_load((ROOT / "configs" / "experiment-policy.yaml").read_text(encoding="utf-8"))
        telemetry = yaml.safe_load((ROOT / "configs" / "telemetry.yaml").read_text(encoding="utf-8"))
        self.assertTrue(policy["approval"]["required"])
        self.assertEqual(policy["scope"]["environment"], "local-only")
        self.assertIn("mongodb", policy["scope"]["denied_targets"])
        candidate = json.loads((ROOT / "schemas" / "examples" / "experiment-restart-gateway.json").read_text(encoding="utf-8"))
        self.assertLessEqual(candidate["risk_score"], policy["max_risk_score"])
        self.assertEqual(set(telemetry["ditto"]["metrics_targets"]), {"gateway", "policies", "things", "things-search", "connectivity"})

    def test_all_yaml_files_parse(self):
        # Skip ignored third-party/runtime trees (upstream Ditto checkout contains Helm templates).
        skipped = {"vendor", ".local", "data", ".venv", "venv", "node_modules", ".git"}
        yaml_files = [
            path for path in [*ROOT.glob("**/*.yaml"), *ROOT.glob("**/*.yml")]
            if not skipped.intersection(path.relative_to(ROOT).parts)
        ]
        self.assertGreaterEqual(len(yaml_files), 6)
        for path in yaml_files:
            with self.subTest(path=path.relative_to(ROOT)):
                yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeTagLoader)

    def test_openapi_contract_is_versioned_and_covers_control_flow(self):
        contract = yaml.safe_load((ROOT / "schemas" / "openapi.yaml").read_text(encoding="utf-8"))
        self.assertEqual(contract["openapi"], "3.1.0")
        self.assertEqual(contract["info"]["version"], "1.0.0")
        self.assertTrue(contract["servers"][0]["url"].startswith("http://127.0.0.1:"))
        for path in (
            "/api/v1/experiments/plan",
            "/api/v1/experiments/{experiment_id}/approval",
            "/api/v1/experiments/{experiment_id}/execute",
            "/api/v1/runs/{run_id}",
        ):
            self.assertIn(path, contract["paths"])

    def test_local_markdown_links_resolve(self):
        markdown_files = [*ROOT.glob("*.md"), *ROOT.glob("docs/**/*.md"), *ROOT.glob(".github/**/*.md")]
        link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
        for markdown_file in markdown_files:
            for target in link_pattern.findall(markdown_file.read_text(encoding="utf-8")):
                if target.startswith(("https://", "http://", "mailto:")):
                    continue
                local_path = target.split("#", 1)[0]
                if local_path:
                    resolved = (markdown_file.parent / local_path).resolve()
                    with self.subTest(source=markdown_file.relative_to(ROOT), target=target):
                        self.assertTrue(resolved.exists(), f"Broken local link: {target}")

    def test_experiment_schema_rejects_unsafe_values(self):
        schema = json.loads((ROOT / "schemas" / "experiment.schema.json").read_text(encoding="utf-8"))
        example = json.loads((ROOT / "schemas" / "examples" / "experiment-restart-gateway.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        for updates in ({"approval_required": False}, {"duration_seconds": 61}, {"action": "run_shell"}):
            with self.subTest(updates=updates):
                self.assertFalse(validator.is_valid({**example, **updates}))


if __name__ == "__main__":
    unittest.main()