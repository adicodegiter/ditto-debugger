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
        )