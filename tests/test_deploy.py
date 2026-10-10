import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "deploy" / "ditto" / "docker-compose.yml"
PINS_FILE = ROOT / "deploy" / "image-pins.env"
NGINX_CONF = ROOT / "deploy" / "ditto" / "nginx.conf"

VAR_WITH_DEFAULT = re.compile(r"\$\{(\w+):-([^}]+)\}")


def read_pins() -> dict[str, str]:
    pins = {}
    for line in PINS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            pins[key.strip()] = value.strip()
    return pins


def compose_services() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))["services"]


class DeployTests(unittest.TestCase):
    def test_every_image_is_pinned_and_never_latest(self):
        for name, service in compose_services().items():
            image = service["image"]
            with self.subTest(service=name):
                self.assertNotIn("latest", image)
                tag = VAR_WITH_DEFAULT.sub(lambda m: m.group(2), image).rsplit(":", 1)
                self.assertEqual(len(tag), 2, f"{name} image has no tag: {image}")
                self.assertRegex(tag[1], r"^v?\d+(\.\d+){1,2}(-[\w.]+)?$")

    def test_compose_fallbacks_match_image_pins(self):
        pins = read_pins()
        self.assertEqual(pins["DITTO_VERSION"], "3.9.8")
        used = set()
        for name, service in compose_services().items():
            for var, default in VAR_WITH_DEFAULT.findall(service["image"]):
                used.add(var)
                with self.subTest(service=name, var=var):
                    self.assertIn(var, pins, f"{var} missing from deploy/image-pins.env")
                    self.assertEqual(default, pins[var], "compose fallback differs from image-pins.env")
        self.assertEqual(used, set(pins), "image-pins.env and compose file reference different variables")

    def test_published_ports_bind_loopback_only(self):
        for name, service in compose_services().items():
            for port in service.get("ports", []):
                with self.subTest(service=name, port=port):
                    self.assertTrue(str(port).startswith("127.0.0.1:"), f"{name} publishes {port} beyond loopback")

    def test_nginx_only_proxies_to_defined_services(self):
        # nginx refuses to start if a proxy_pass host does not resolve.
        services = set(compose_services())
        hosts = set(re.findall(r"proxy_pass\s+https?://([\w-]+)", NGINX_CONF.read_text(encoding="utf-8")))
        self.assertTrue(hosts)
        self.assertLessEqual(hosts, services, f"nginx proxies to undefined services: {hosts - services}")

    def test_smoke_test_has_help_and_loopback_default(self):
        env = {k: v for k, v in os.environ.items() if k != "DITTO_BASE_URL"}
        result = subprocess.run(
            [sys.executable, str(ROOT / "simulator" / "smoke_test.py"), "--help"],
            capture_output=True, text=True, timeout=60, env=env, cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)
        # The smoke test replaces its whole twin, so it must never use a fleet vehicle ID.
        self.assertIn("smoke-test", result.stdout)
        self.assertNotIn("vehicle-", result.stdout)
        sys.path.insert(0, str(ROOT))
        try:
            from simulator import config
        finally:
            sys.path.remove(str(ROOT))
        if "DITTO_BASE_URL" not in os.environ:
            self.assertEqual(config.DITTO_BASE_URL, "http://127.0.0.1:8080")


if __name__ == "__main__":
    unittest.main()
