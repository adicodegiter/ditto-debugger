"""Operator CLI: the only way to execute an approved experiment.

Runs in its own process, never inside the API. Requires CHAOS_EXECUTION_ENABLED=true
for real execution; --dry-run reads real Docker state and real SLI samples but never
changes a container.

    python -m safety.cli baseline                       # capture a baseline from live SLI samples
    python -m safety.cli plan BASELINE_ID               # ranked candidates from the planner
    python -m safety.cli approve EXPERIMENT_ID --by NAME --reason TEXT   # interactive: type APPROVE
    python -m safety.cli execute EXPERIMENT_ID --dry-run
    CHAOS_EXECUTION_ENABLED=true python -m safety.cli execute EXPERIMENT_ID
    CHAOS_EXECUTION_ENABLED=true python -m safety.cli execute EXPERIMENT_ID --forced-test
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

    def arm(self, target: str, deadline_epoch: float, action: str = "restart_container", state: Any = None) -> None:
        return None

    def disarm(self, handle: Any) -> None:
        return None


def build_runner(settings: Settings, orchestrator: AgentOrchestrator, dry_run: bool) -> tuple[ExperimentRunner, Any]:
    policy = orchestrator.policy
    real = SubprocessDockerAdapter(policy.compose_project, settings.compose_file, settings.image_pins_file)
    docker = DryRunDockerAdapter(real) if dry_run else real
    observations = SliWindowObservationProvider(settings.metrics_dir, real, policy.thresholds.window_seconds)
    recovery = HttpRecoveryChecker(settings.ditto_base_url, settings.ditto_username, settings.ditto_password)
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


def _orchestrator() -> AgentOrchestrator:
    return AgentOrchestrator(Settings.from_environment())


def cmd_baseline(args: argparse.Namespace) -> int:
    orchestrator = _orchestrator()
    try:
        baseline = orchestrator.capture_baseline()
    except ValueError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 2
    write = baseline.get("telemetry", {}).get("telemetry", {}).get("write", {})
    print(json.dumps({
        "baseline_id": baseline["baseline_id"],
        "captured_at": baseline["captured_at"],
        "telemetry_status": baseline["telemetry_status"],
        "write_samples": write.get("samples"),
        "write_error_rate": write.get("error_rate"),
        "write_p95_ms": (write.get("latency_ms") or {}).get("p95"),
    }, indent=2))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    orchestrator = _orchestrator()
    try:
        plan = orchestrator.plan_experiments(args.baseline_id, args.max_candidates)
    except (LookupError, ValueError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 2
    for index, candidate in enumerate(plan["candidates"], 1):
        print(f"{index}. {candidate['id']}")
        print(f"   {candidate['target_component']} {candidate['action']} {candidate['duration_seconds']}s  "
              f"risk={candidate['risk_score']}  predicted={candidate.get('predicted_slis', candidate['predicted_impacts'])}")
        if candidate.get("reason"):
            print(f"   reason: {candidate['reason']}")
    for rejection in plan.get("policy_rejections", []):
        print(f"   not planned: {rejection['target']}: {rejection['reason']}")
    return 0 if plan["candidates"] else 1


def cmd_approve(args: argparse.Namespace) -> int:
    orchestrator = _orchestrator()
    experiment = orchestrator.get_experiment(args.experiment_id)
    if experiment is None:
        print("Refused: experiment not found.", file=sys.stderr)
        return 2
    print(f"Target    : {experiment['target_component']}")
    print(f"Action    : {experiment['action']} for {experiment['duration_seconds']} s")
    print(f"Hypothesis: {experiment['hypothesis']}")
    print(f"Predicted : {experiment.get('predicted_slis', experiment['predicted_impacts'])}")
    print(f"Hash      : {experiment['candidate_hash']}")
    # A human approval must be typed by a human: no --yes flag, and no piped or scripted stdin.
    if not sys.stdin.isatty():
        print("Refused: approvals must be typed by a human at an interactive terminal.", file=sys.stderr)
        return 2
    if input("Type APPROVE to record this human approval: ").strip() != "APPROVE":
        print("Not approved.")
        return 1
    try:
        approval = orchestrator.record_approval(
            args.experiment_id, "approve", f"approved_by={args.by}; {args.reason}", experiment["candidate_hash"]
        )
    except (LookupError, ValueError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(approval, indent=2))
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    settings = Settings.from_environment()
    if not args.dry_run and not settings.execution_enabled:
        print("Refusing to execute: set CHAOS_EXECUTION_ENABLED=true (or use --dry-run).", file=sys.stderr)
        return 3
    orchestrator = AgentOrchestrator(settings)
    try:
        candidate, approval, baseline = orchestrator.prepare_execution(args.experiment_id)
        runner, docker = build_runner(settings, orchestrator, args.dry_run)
        run = runner.execute(candidate, approval, baseline, forced_test=args.forced_test, dry_run=args.dry_run)
    except (LookupError, RunnerError, SafetyViolation) as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 2
    if args.dry_run:
        run["dry_run_commands"] = [" ".join(command) for command in docker.commands]
        run["diagnosis"] = orchestrator.diagnose_run(run, candidate, baseline)  # shown, never recorded
    else:
        run["findings"] = orchestrator.record_operator_run(run, candidate, baseline)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = ("-dry-run" if args.dry_run else "") + ("-forced-test" if args.forced_test else "")
    record_path = out_dir / f"{run['run_id']}{suffix}.json"
    record_path.write_text(json.dumps(run, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(run, indent=2, sort_keys=True))
    print(f"Saved: {record_path}", file=sys.stderr)
    if args.forced_test:
        return 0 if run.get("forced_test_passed") and run.get("recovery_verified") else 1
    return 0 if run["status"] == "completed" else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operator CLI for the deterministic chaos control plane.")
    sub = parser.add_subparsers(dest="command", required=True)

    baseline = sub.add_parser("baseline", help="capture a baseline from the live SLI samples in METRICS_DIR")
    baseline.set_defaults(handler=cmd_baseline)

    plan = sub.add_parser("plan", help="rank candidate experiments for a baseline")
    plan.add_argument("baseline_id")
    plan.add_argument("--max-candidates", type=int, default=6)
    plan.set_defaults(handler=cmd_plan)

    approve = sub.add_parser("approve", help="record a human approval for one immutable candidate")
    approve.add_argument("experiment_id")
    approve.add_argument("--by", required=True, help="name of the approving human")
    approve.add_argument("--reason", required=True)
    approve.set_defaults(handler=cmd_approve)

    execute = sub.add_parser("execute", help="run one approved, immutable experiment")
    execute.add_argument("experiment_id", help="ID of an approved experiment candidate")
    execute.add_argument("--dry-run", action="store_true", help="read real state and SLIs; change no container; keep the approval")
    execute.add_argument("--forced-test", action="store_true",
                         help="kill-switch proof: lower the write-p95 limit during the fault so the real kill switch must fire; never scored")
    execute.add_argument("--out", default=str(REPO_ROOT / "data" / "experiments"), help="directory for the run record JSON")
    execute.set_defaults(handler=cmd_execute)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
