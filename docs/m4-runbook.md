# M4 Runbook: First Real Experiment and Forced Kill-Switch Proof

CLAUDE.md M4 (the brief's Day-1 minimum) needs:
- one short, approved, isolated experiment with rollback and a max duration, and
- a **forced abort that proves the kill switch**.

Everything runs from the repository root on the machine that hosts the pinned Ditto stack.

## 0. Preconditions

```bash
bash deploy/health_check.sh            # OVERALL: HEALTHY
python -m unittest discover -s tests   # all green
```

- The policy (`configs/experiment-policy.yaml`) allows only `gateway` and `things-search`, with a 60 s maximum. `mongodb` is denied.
- Nothing changes a container unless `CHAOS_EXECUTION_ENABLED=true` is set **and** the run has a recorded human approval.

## 1. Start the workload (own terminal, keep it running)

```bash
python simulator/fleet_workload.py --duration-seconds 1200 --metrics-dir data/metrics
```

Wait **at least 120 s** before capturing the baseline (CLAUDE.md M2).

## 2. Baseline, plan, approve

```bash
python -m safety.cli baseline                    # prints baseline_id; refused unless every SLI is healthy
python -m safety.cli plan <baseline_id>          # ranked candidates with predicted SLI effects
python -m safety.cli approve <experiment_id> --by "<your name>" --reason "M4 first experiment"
```

`approve` shows the target, action, duration, hypothesis, prediction and candidate hash, then asks you to type `APPROVE`. Approvals expire after 10 minutes and can be used for only one real run.

## 3. Rehearse (changes nothing, keeps the approval)

```bash
python -m safety.cli execute <experiment_id> --dry-run
python -m safety.cli execute <experiment_id> --dry-run --forced-test
```

Expected results:
- **Normal dry run:** `status: completed` and `recovery_verified: true`, with `dry_run_commands` showing exactly the restart and start commands.
- **Forced dry run:** `forced_test_passed: true` and `abort_reason: p95_latency_above_baseline_multiplier`.

## 4. Real experiment

```bash
CHAOS_EXECUTION_ENABLED=true python -m safety.cli execute <experiment_id>
```

The runner then:
1. Runs preflight: project, target and running state.
2. Checks steady state on the last 30 s of real SLIs.
3. Arms the independent watchdog.
4. Restarts the target.
5. Polls the kill switch every 2 s.
6. Rolls back (`docker compose start`).
7. Checks recovery: `/health`, an authenticated canary, and the SLIs since rollback back within thresholds.
8. Saves the run, its findings and `data/experiments/<run_id>.json`.

## 5. Forced kill-switch proof

An approval can be used only once, so capture a **new** baseline and plan, then approve the new candidate:

```bash
python -m safety.cli baseline
python -m safety.cli plan <new_baseline_id>
python -m safety.cli approve <new_experiment_id> --by "<your name>" --reason "M4 forced kill-switch proof"
CHAOS_EXECUTION_ENABLED=true python -m safety.cli execute <new_experiment_id> --forced-test
```

`--forced-test` lowers the write-p95 limit to 1% of the baseline **during the fault only**. Real write latency therefore has to trip the real kill switch, which then rolls back through the real path. Steady state and recovery keep the normal thresholds. The run record has:
- `forced_test: true`
- `forced_test_passed: true` (the kill switch fired during the fault)
- `abort_reason`
- timestamps showing abort → rollback
- `resilience_score.status: not_scored_forced_test`, so it is never used as resilience evidence

## Evidence to keep

- `data/experiments/<run_id>.json` for both runs. This is the result JSON CLAUDE.md asks for.
- `data/experiments/watchdog.jsonl`, the watchdog arm and disarm log.
- The audit trail, through `orchestrator.list_audit()`: preflight, fault start, abort, cleanup and recovery events.

## If something goes wrong

- **The runner crashes mid-fault:** the watchdog runs `docker compose start <target>` at the deadline plus 15 s.
- **Manual recovery:** `docker compose --env-file deploy/image-pins.env -f deploy/ditto/docker-compose.yml start things-search`, then `bash deploy/health_check.sh --wait 300`.
