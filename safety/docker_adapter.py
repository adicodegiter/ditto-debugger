"""The only code that changes containers. Used by the operator CLI and the watchdog, never by the API.

Every fault is a fixed (capture -> apply -> rollback) recipe in this file. The policy can only
enable names listed in ACTIONS (safety.policy.SUPPORTED_ACTIONS); targets are validated against
the policy before anything runs and are always passed as a single argv element. Every rollback
is idempotent, so the runner and the independent watchdog can both call it safely.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
CFS_PERIOD_US = 100_000
BURST_METRICS_DIR = REPO_ROOT / "data" / "metrics-burst"


class DockerCommandError(RuntimeError):
    """A fixed Docker command failed or returned something unexpected."""


# Human-readable description of each fault; the recipes live in SubprocessDockerAdapter.
ACTIONS = {
    "restart_container": "docker compose restart <target>; rollback: docker compose start <target>",
    "stop_container": "docker compose stop <target>; rollback: docker compose start <target>",
    "pause_container": "docker compose pause <target> (freezes all processes); rollback: docker compose unpause <target>",
    "cpu_throttle": "docker update --cpu-period/--cpu-quota on the target container; rollback: restore the captured quota/period",
    "traffic_burst": "a bounded extra fleet workload against the API (ends by itself); rollback: terminate it early",
}
# Kept for older imports.
ACTION_COMMANDS = {"restart_container": "restart", "stop_container": "stop", "pause_container": "pause"}

_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")


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


def burst_command(params: dict[str, Any]) -> list[str]:
    """The fixed traffic-burst command. Bounded by its own --duration-seconds."""
    duration = int(params["duration_seconds"])
    return [
        sys.executable, str(REPO_ROOT / "simulator" / "fleet_workload.py"),
        "--duration-seconds", str(duration),
        "--rate-per-second", str(float(params.get("rate_per_second", 10.0))),
        "--vehicles", str(int(params.get("vehicles", 16))),
        "--vehicle-offset", "100",
        "--metrics-dir", str(BURST_METRICS_DIR),
        "--no-container-metrics",
        "--freshness-method", "poll-proxy",
    ]


class SubprocessDockerAdapter:
    def __init__(
        self,
        project: str,
        compose_file: Path,
        env_file: Path | None,
        executable: str = "docker",
        run: Callable[..., Any] = subprocess.run,
        popen: Callable[..., Any] = subprocess.Popen,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.project = project
        self.compose_file = Path(compose_file)
        self.env_file = Path(env_file) if env_file else None
        self.executable = executable
        self._run_process = run
        self._popen = popen
        self.timeout_seconds = timeout_seconds
        self._burst: Any = None

    # ------------------------------------------------------------------ plumbing
    def base_command(self) -> list[str]:
        command = [self.executable, "compose", "-p", self.project]
        if self.env_file is not None:
            command += ["--env-file", str(self.env_file)]
        return command + ["-f", str(self.compose_file)]

    def _exec(self, argv: list[str], what: str) -> str:
        try:
            result = self._run_process(argv, check=True, capture_output=True, text=True, timeout=self.timeout_seconds)
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or "").strip().splitlines()[-1:] or [""]
            raise DockerCommandError(f"{what} failed: {detail[0]}") from error
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DockerCommandError(f"{what} could not run: {type(error).__name__}") from error
        return result.stdout or ""

    def _run(self, *args: str) -> str:
        return self._exec([*self.base_command(), *args], f"docker compose {args[0]}")

    def _container_id(self, target: str) -> str:
        container_id = self._run("ps", "-q", target).strip().splitlines()
        if len(container_id) != 1 or not _CONTAINER_ID.match(container_id[0]):
            raise DockerCommandError("Could not resolve exactly one container for the target.")
        return container_id[0]

    # ------------------------------------------------------------------ reads
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

    def capture(self, action: str, target: str) -> dict[str, Any]:
        """State needed to undo the action exactly (recorded in the run and passed to the watchdog)."""
        if action == "cpu_throttle":
            container_id = self._container_id(target)
            raw = self._exec([self.executable, "inspect", "-f", "{{.HostConfig.CpuQuota}} {{.HostConfig.CpuPeriod}}", container_id], "docker inspect")
            quota, period = (int(part) for part in raw.split())
            return {"container_id": container_id, "cpu_quota": quota, "cpu_period": period}
        return {}

    # ------------------------------------------------------------------ fault + rollback
    def apply(self, action: str, target: str, params: dict[str, Any] | None = None, state: dict[str, Any] | None = None) -> None:
        params = params or {}
        if action in ("restart_container", "stop_container", "pause_container"):
            self._run(ACTION_COMMANDS[action], target)
        elif action == "cpu_throttle":
            container_id = (state or {}).get("container_id") or self._container_id(target)
            quota = max(1000, int(float(params["cpus"]) * CFS_PERIOD_US))
            self._exec([self.executable, "update", "--cpu-period", str(CFS_PERIOD_US), "--cpu-quota", str(quota), container_id], "docker update")
        elif action == "traffic_burst":
            BURST_METRICS_DIR.mkdir(parents=True, exist_ok=True)
            self._burst = self._popen(burst_command(params), cwd=str(REPO_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            raise DockerCommandError(f"Unsupported action: {action}")

    def rollback(self, action: str, target: str, state: dict[str, Any] | None = None) -> None:
        state = state or {}
        if action in ("restart_container", "stop_container"):
            self._run("start", target)
        elif action == "pause_container":
            if self.service_states().get(target, {}).get("state") == "paused":
                self._run("unpause", target)
            self._run("start", target)
        elif action == "cpu_throttle":
            container_id = state.get("container_id") or self._container_id(target)
            quota = int(state.get("cpu_quota", 0))
            period = int(state.get("cpu_period", 0))
            if quota <= 0:
                # `--cpus 0` does NOT remove a limit (verified); quota -1 means unlimited.
                argv = [self.executable, "update", "--cpu-quota", "-1", container_id]
            else:
                argv = [self.executable, "update", "--cpu-period", str(period or CFS_PERIOD_US), "--cpu-quota", str(quota), container_id]
            self._exec(argv, "docker update (restore)")
        elif action == "traffic_burst":
            if self._burst is not None and self._burst.poll() is None:
                self._burst.terminate()
                try:
                    self._burst.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._burst.kill()
            # A burst started by a crashed runner ends by itself (bounded duration).
        else:
            raise DockerCommandError(f"Unsupported action: {action}")

    def start(self, target: str) -> None:
        """Kept for older callers: idempotent start of a stopped service."""
        self._run("start", target)


class DryRunDockerAdapter:
    """Reads real state but never changes it; records the commands it would have run."""

    def __init__(self, real: SubprocessDockerAdapter) -> None:
        self.real = real
        self.commands: list[list[str]] = []

    def service_states(self) -> dict[str, dict[str, str]]:
        return self.real.service_states()

    def preflight(self, target: str) -> None:
        self.real.preflight(target)

    def capture(self, action: str, target: str) -> dict[str, Any]:
        return self.real.capture(action, target)

    def apply(self, action: str, target: str, params: dict[str, Any] | None = None, state: dict[str, Any] | None = None) -> None:
        params = params or {}
        base = self.real.base_command()
        if action in ACTION_COMMANDS:
            self.commands.append([*base, ACTION_COMMANDS[action], target])
        elif action == "cpu_throttle":
            quota = max(1000, int(float(params["cpus"]) * CFS_PERIOD_US))
            self.commands.append([self.real.executable, "update", "--cpu-period", str(CFS_PERIOD_US), "--cpu-quota", str(quota), (state or {}).get("container_id", target)])
        elif action == "traffic_burst":
            self.commands.append(burst_command(params))
        else:
            raise DockerCommandError(f"Unsupported action: {action}")

    def rollback(self, action: str, target: str, state: dict[str, Any] | None = None) -> None:
        base = self.real.base_command()
        if action == "pause_container":
            self.commands.append([*base, "unpause", target])
        elif action == "cpu_throttle":
            self.commands.append([self.real.executable, "update", "--cpu-quota", "-1" if int((state or {}).get("cpu_quota", 0)) <= 0 else str(state["cpu_quota"]), (state or {}).get("container_id", target)])
        elif action == "traffic_burst":
            self.commands.append(["terminate", "traffic-burst"])
        else:
            self.commands.append([*base, "start", target])

    def start(self, target: str) -> None:
        self.commands.append([*self.real.base_command(), "start", target])
