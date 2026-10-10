"""The only code that runs Docker. Used by the operator CLI and the watchdog, never by the API."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any, Callable


class DockerCommandError(RuntimeError):
    """A fixed Docker Compose command failed or returned something unexpected."""


# Fixed action -> Compose subcommand mapping. The policy can only enable names listed
# here (safety.policy.SUPPORTED_ACTIONS); targets are validated against the allow-list
# before any command is built, and are always passed as a single argv element.
ACTION_COMMANDS = {
    "restart_container": "restart",
    "stop_container": "stop",
    "pause_container": "pause",
}

ROLLBACK_COMMANDS = {
    "restart_container": "start",
    "stop_container": "start",
    "pause_container": "unpause",
}


def parse_ps_json(stdout: str) -> dict[str, dict[str, str]]:
    """Parse `docker compose ps --format json` (one JSON object per line, or one array)."""
    text = stdout.strip()
    if not text:
        return {}
    if text.startswith("["):
        rows = json.loads(text)
    else:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    states: dict[str, dict[str, str]] = {}
    for row in rows:
        service = row.get("Service")
        if service:
            states[str(service)] = {"state": str(row.get("State", "")), "health": str(row.get("Health", "") or "")}
    return states


class SubprocessDockerAdapter:
    def __init__(
        self,
        project: str,
        compose_file: Path,
        env_file: Path | None,
        executable: str = "docker",
        run: Callable[..., Any] = subprocess.run,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.project = project
        self.compose_file = Path(compose_file)
        self.env_file = Path(env_file) if env_file else None
        self.executable = executable
        self._run_process = run
        self.timeout_seconds = timeout_seconds

    def base_command(self) -> list[str]:
        command = [self.executable, "compose", "-p", self.project]
        if self.env_file is not None:
            command += ["--env-file", str(self.env_file)]
        return command + ["-f", str(self.compose_file)]

    def _run(self, *args: str) -> str:
        try:
            result = self._run_process(
                [*self.base_command(), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or "").strip().splitlines()[-1:] or [""]
            raise DockerCommandError(f"docker compose {args[0]} failed: {detail[0]}") from error
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DockerCommandError(f"docker compose {args[0]} could not run: {type(error).__name__}") from error
        return result.stdout or ""

    def service_states(self) -> dict[str, dict[str, str]]:
        return parse_ps_json(self._run("ps", "-a", "--format", "json"))

    def preflight(self, target: str) -> None:
        if not self.compose_file.is_file():
            raise DockerCommandError(f"Compose file not found: {self.compose_file}")
        config = json.loads(self._run("config", "--format", "json"))
        if config.get("name") != self.project:
            raise DockerCommandError("The Compose file does not belong to the policy's project.")
        if target not in config.get("services", {}):
            raise DockerCommandError("The target is not a service in the pinned Compose project.")
        state = self.service_states().get(target, {})
        if state.get("state") != "running":
            raise DockerCommandError("The target is not running before the experiment.")

    def apply(self, action: str, target: str) -> None:
        subcommand = ACTION_COMMANDS.get(action)
        if subcommand is None:
            raise DockerCommandError(f"Unsupported action: {action}")
        self._run(subcommand, target)

    def restore(self, action: str, target: str) -> None:
        command = ROLLBACK_COMMANDS.get(action)
        if command is None:
            raise DockerCommandError(f"Unsupported rollback action: {action}")
        self._run(command, target)

    def start(self, target: str) -> None:
        """Compatibility helper for callers that only support start-based cleanup."""
        self.restore("stop_container", target)


class DryRunDockerAdapter:
    """Reads real state but never changes it; records the commands it would have run."""

    def __init__(self, real: SubprocessDockerAdapter) -> None:
        self.real = real
        self.commands: list[list[str]] = []

    def service_states(self) -> dict[str, dict[str, str]]:
        return self.real.service_states()

    def preflight(self, target: str) -> None:
        self.real.preflight(target)

    def apply(self, action: str, target: str) -> None:
        subcommand = ACTION_COMMANDS.get(action)
        if subcommand is None:
            raise DockerCommandError(f"Unsupported action: {action}")
        self.commands.append([*self.real.base_command(), subcommand, target])

    def restore(self, action: str, target: str) -> None:
        command = ROLLBACK_COMMANDS.get(action)
        if command is None:
            raise DockerCommandError(f"Unsupported rollback action: {action}")
        self.commands.append([*self.real.base_command(), command, target])

    def start(self, target: str) -> None:
        self.restore("stop_container", target)
