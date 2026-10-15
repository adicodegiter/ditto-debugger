from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    compose_file: Path
    kubernetes_manifests: tuple[Path, ...]
    prometheus_url: str
    database_path: Path
    policy_path: Path
    execution_enabled: bool = False
    ditto_base_url: str = "http://127.0.0.1:8080"
    metrics_dir: Path = REPOSITORY_ROOT / "data" / "metrics"
    # Local sample credentials by default (upstream deployment's ditto:ditto, loopback only).
    ditto_username: str = "ditto"
    ditto_password: str = "ditto"
    image_pins_file: Path = REPOSITORY_ROOT / "deploy" / "image-pins.env"
    # The telemetry agent refuses to contact a non-loopback Prometheus unless this is explicitly
    # enabled. Keep it false for local-only use; set PROMETHEUS_ALLOW_REMOTE=true only when
    # pointing at a Prometheus you control (e.g. a deployed instance with auth in front of it).
    workload_mode: str = "live"
    prometheus_allow_remote: bool = False

    @classmethod
    def from_environment(cls) -> "Settings":
        compose_file = Path(
            os.environ.get(
                "DITTO_COMPOSE_FILE",
                str(REPOSITORY_ROOT / "deploy" / "ditto" / "docker-compose.yml"),
            )
        ).expanduser()
        manifest_value = os.environ.get("DITTO_KUBERNETES_MANIFESTS", "")
        manifests = tuple(Path(item).expanduser() for item in manifest_value.split(os.pathsep) if item.strip())
        return cls(
            compose_file=compose_file,
            kubernetes_manifests=manifests,
            prometheus_url=os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090").rstrip("/"),
            database_path=Path(os.environ.get("DATABASE_PATH", str(REPOSITORY_ROOT / ".local" / "workbench.sqlite3"))).expanduser(),
            policy_path=Path(os.environ.get("EXPERIMENT_POLICY_FILE", str(REPOSITORY_ROOT / "configs" / "experiment-policy.yaml"))).expanduser(),
            execution_enabled=os.environ.get("CHAOS_EXECUTION_ENABLED", "false").lower() == "true",
            ditto_base_url=os.environ.get("DITTO_BASE_URL", "http://127.0.0.1:8080").rstrip("/"),
            metrics_dir=Path(os.environ.get("METRICS_DIR", str(REPOSITORY_ROOT / "data" / "metrics"))).expanduser(),
            # Accept both spellings used in this repo (simulator/ uses DITTO_USER / DITTO_PASS).
            ditto_username=os.environ.get("DITTO_USERNAME") or os.environ.get("DITTO_USER") or "ditto",
            ditto_password=os.environ.get("DITTO_PASSWORD") or os.environ.get("DITTO_PASS") or "ditto",
            image_pins_file=Path(os.environ.get("DITTO_IMAGE_PINS", str(REPOSITORY_ROOT / "deploy" / "image-pins.env"))).expanduser(),
            prometheus_allow_remote=os.environ.get("PROMETHEUS_ALLOW_REMOTE", "false").lower() == "true",
            # "live" probes a real Ditto (default). "simulated" writes clearly labelled synthetic
            # samples (source=simulated) so a demo deployment can exercise baseline -> plan without
            # a Ditto. Anything else falls back to "live" so a typo can never enable simulation.
            workload_mode=(os.environ.get("WORKLOAD_MODE", "live") or "live").strip().lower()
            if (os.environ.get("WORKLOAD_MODE", "live") or "live").strip().lower() in {"live", "simulated"}
            else "live",
        )