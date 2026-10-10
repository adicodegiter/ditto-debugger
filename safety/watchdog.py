"""Independent max-duration watchdog.

Armed by the runner *before* a fault is injected, as a separate, detached OS process.
At the deadline it runs the fixed, idempotent cleanup (`docker compose start <target>`)
whether or not the runner is still alive. The runner disarms it after its own cleanup
succeeds; if cleanup fails, the watchdog stays armed and retries.

    python -m safety.watchdog --help
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from safety.docker_adapter import DockerCommandError, SubprocessDockerAdapter  # noqa: E402
from safety.policy import Policy  # noqa: E402

DEFAULT_LOG = REPO_ROOT / "data" / "experiments" / "watchdog.jsonl"


def _log(path: Path, event: str, **fields: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


class ProcessWatchdog:
    """Starts/stops the watchdog as a detached process that outlives the runner."""

    def __init__(self, policy_path: Path, compose_file: Path, env_file: Path | None, log_path: Path = DEFAULT_LOG) -> None:
        self.policy_path = Path(policy_path)
        self.compose_file = Path(compose_file)
        self.env_file = Path(env_file) if env_file else None
        self.log_path = Path(log_path)

    def arm(self, target: str, deadline_epoch: float, action: str = "restart_container", state: dict[str, Any] | None = None) -> subprocess.Popen:
        state = state or {}
        command = [
            sys.executable, "-m", "safety.watchdog",
            "--target", target,
            "--action", action,
            "--deadline-epoch", f"{deadline_epoch:.3f}",
            "--policy", str(self.policy_path),
            "--compose-file", str(self.compose_file),
            "--log", str(self.log_path),
        ]
        if self.env_file is not None:
            command += ["--env-file", str(self.env_file)]
        if "cpu_quota" in state:
            command += ["--cpu-quota", str(int(state["cpu_quota"])), "--cpu-period", str(int(state.get("cpu_period", 0))),
                        "--container-id", str(state.get("container_id", ""))]
        kwargs: dict[str, Any] = {"cwd": str(REPO_ROOT), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(command, **kwargs)

    def disarm(self, handle: subprocess.Popen) -> None:
        if handle.poll() is None:
            handle.terminate()
            try:
                handle.wait(timeout=5)
            except subprocess.TimeoutExpired:
                handle.kill()
        _log(self.log_path, "disarmed", pid=handle.pid)


def run_watchdog(args: argparse.Namespace, sleep=time.sleep, clock=time.time, adapter: Any = None) -> int:
    policy = Policy.from_file(args.policy)
    action = getattr(args, "action", "restart_container")
    explicit = dict(policy.action_targets).get(action)
    if (
        action not in policy.enabled_actions
        or args.target not in policy.targets_for(action)
        or (args.target in policy.denied_targets and (explicit is None or args.target not in explicit))
    ):
        _log(args.log, "refused", target=args.target, action=action, reason="target/action not allowlisted")
        return 2
    _log(args.log, "armed", target=args.target, action=action, deadline_epoch=args.deadline_epoch, pid=os.getpid())
    while (remaining := args.deadline_epoch - clock()) > 0:
        sleep(min(remaining, 1.0))
    if action == "traffic_burst":
        # The burst process is bounded by its own --duration-seconds; nothing to undo here.
        _log(args.log, "fired_cleanup", target=args.target, action=action, result="bounded_by_design")
        return 0
    state: dict[str, Any] = {}
    if getattr(args, "cpu_quota", None) is not None:
        state = {"cpu_quota": args.cpu_quota, "cpu_period": args.cpu_period or 0, "container_id": args.container_id or None}
    docker = adapter or SubprocessDockerAdapter(policy.compose_project, args.compose_file, args.env_file)
    for attempt in range(1, args.attempts + 1):
        try:
            docker.rollback(action, args.target, state)
            _log(args.log, "fired_cleanup", target=args.target, action=action, attempt=attempt, result="rolled_back")
            return 0
        except DockerCommandError as error:
            _log(args.log, "fired_cleanup", target=args.target, attempt=attempt, result="failed", error=str(error))
            sleep(5)
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Independent max-duration watchdog for one chaos experiment.")
    parser.add_argument("--target", required=True, help="allowlisted Compose service to restore at the deadline")
    parser.add_argument("--action", default="restart_container", help="the fault action whose rollback to run")
    parser.add_argument("--cpu-quota", type=int, default=None, help="cpu_throttle: original CFS quota to restore")
    parser.add_argument("--cpu-period", type=int, default=None, help="cpu_throttle: original CFS period to restore")
    parser.add_argument("--container-id", default=None, help="cpu_throttle: container to restore")
    parser.add_argument("--deadline-epoch", type=float, required=True, help="UNIX time at which to run cleanup")
    parser.add_argument("--policy", type=Path, default=REPO_ROOT / "configs" / "experiment-policy.yaml")
    parser.add_argument("--compose-file", type=Path, default=REPO_ROOT / "deploy" / "ditto" / "docker-compose.yml")
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / "deploy" / "image-pins.env")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="JSON Lines log of watchdog events")
    parser.add_argument("--attempts", type=int, default=3, help="cleanup attempts before giving up")
    return parser


if __name__ == "__main__":
    raise SystemExit(run_watchdog(_parser().parse_args()))
