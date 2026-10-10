"""Operator CLI: the only way to execute an approved experiment.

Runs in its own process, never inside the API. Requires CHAOS_EXECUTION_ENABLED=true
for real execution; --dry-run reads real Docker state and real SLI samples but never
changes a container.

    python -m safety.cli execute EXPERIMENT_ID --dry-run
    CHAOS_EXECUTION_ENABLED=true python -m safety.cli execute EXPERIMENT_ID
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for entry in (REPO_ROOT, REPO_ROOT / "backend"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from app.orchestrator import AgentOrchestrator  # noqa: E402
from app.settings import Settings  # noqa: E402
from safety.docker_adapter import DryRunDockerAdapter, SubprocessDockerAdapter  # noqa: E402
from safety.observations import SliWindowObservationProvider  # noqa: E402
from safety.policy import SafetyViolation  # noqa: E402
from safety.recovery import HttpRecoveryChecker  # noqa: E402
from safety.runner import ExperimentRunner, RunnerError  # noqa: E402
from safety.watchdog import ProcessWatchdog  # noqa: E402


class NoopWatchdog:
    """Dry runs change nothing, so there is nothing for a watchdog to undo."""

    def arm(self, target: str, deadline_epoch: float) -> None:
        return None

    def disarm(self, handle: Any) -> None:
        return None


def build_runner(settings: Settings, orchestrator: AgentOrchestrator, dry_run: bool) -> tuple[ExperimentRunner, Any]:
    policy = orchestrator.policy
    real = SubprocessDockerAdapter(policy.compose_project, settings.compose_file, settings.image_pins_file)
    docker = DryRunDockerAdapter(real) if dry_run else real
    observations = SliWindowObservationProvider(settings.metrics_dir, real, policy.thresholds.window_seconds)
    recovery = HttpRecoveryChecker(settings.ditto_base_url, settings.ditto_user, settings.ditto_password)
    watchdog = NoopWatchdog() if dry_run else ProcessWatchdog(settings.policy_path, settings.compose_file, settings.image_pins_file)
    runner = ExperimentRunner(
        policy,
        docker,
        observations,
        recovery,
        watchdog,
        audit=orchestrator.repository.record_audit,
        # A dry run must not burn the human approval.
        consume_approval=None if dry_run else orchestrator.repository.consume_approval,
    )
    return runner, docker


def cmd_execute(args: argparse.Namespace) -> int:
    settings = Settings.from_environment()
    if not args.dry_run and not settings.execution_enabled:
        print("Refusing to execute: set CHAOS_EXECUTION_ENABLED=true (or use --dry-run).", file=sys.stderr)
        return 3
    orchestrator = AgentOrchestrator(settings)
    try:
        candidate, approval, baseline = orchestrator.prepare_execution(args.experiment_id)
        runner, docker = build_runner(settings, orchestrator, args.dry_run)
        run = runner.execute(candidate, approval, baseline)
    except (LookupError, RunnerError, SafetyViolation) as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 2
    if args.dry_run:
        run["dry_run"] = True
        run["dry_run_commands"] = [" ".join(command) for command in docker.commands]
    else:
        orchestrator.record_run(run)
    print(json.dumps(run, indent=2, sort_keys=True))
    return 0 if run["status"] == "completed" else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operator CLI for the deterministic chaos control plane.")
    sub = parser.add_subparsers(dest="command", required=True)
    execute = sub.add_parser("execute", help="run one approved, immutable experiment")
    execute.add_argument("experiment_id", help="ID of an approved experiment candidate")
    execute.add_argument("--dry-run", action="store_true", help="read real state and SLIs; change no container; keep the approval")
    execute.set_defaults(handler=cmd_execute)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
