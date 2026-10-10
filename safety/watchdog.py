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

    def arm(self, target: str, deadline_epoch: float) -> subprocess.Popen:
        command = [
            sys.executable, "-m", "safety.watchdog",
            "--target", target,
            "--deadline-epoch", f"{deadline_epoch:.3f}",
            "--policy", str(self.policy_path),
            "--compose-file", str(self.compose_file),
            "--log", str(self.log_path),
        ]
        if self.env_file is not None:
            command += ["--env-file", str(self.env_file)]
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
    if args.target not in policy.allowed_targets or args.target in policy.denied_targets:
        _log(args.log, "refused", target=args.target, reason="target not allowlisted")
        return 2
    _log(args.log, "armed", target=args.target, deadline_epoch=args.deadline_epoch, pid=os.getpid())
    while (remaining := args.deadline_epoch - clock()) > 0:
        sleep(min(remaining, 1.0))
    docker = adapter or SubprocessDockerAdapter(policy.compose_project, args.compose_file, args.env_file)
    for attempt in range(1, args.attempts + 1):
        try:
            docker.start(args.target)
            _log(args.log, "fired_cleanup", target=args.target, attempt=attempt, result="started")
            return 0
        except DockerCommandError as error:
            _log(args.log, "fired_cleanup", target=args.target, attempt=attempt, result="failed", error=str(error))
            sleep(5)
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Independent max-duration watchdog for one chaos experiment.")
    parser.add_argument("--target", required=True, help="allowlisted Compose service to start at the deadline")
    parser.add_argument("--deadline-epoch", type=float, required=True, help="UNIX time at which to run cleanup")
    parser.add_argument("--policy", type=Path, default=REPO_ROOT / "configs" / "experiment-policy.yaml")
    parser.add_argument("--compose-file", type=Path, default=REPO_ROOT / "deploy" / "ditto" / "docker-compose.yml")
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / "deploy" / "image-pins.env")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="JSON Lines log of watchdog events")
    parser.add_argument("--attempts", type=int, default=3, help="cleanup attempts before giving up")
    return parser


if __name__ == "__main__":
    raise SystemExit(run_watchdog(_parser().parse_args()))
