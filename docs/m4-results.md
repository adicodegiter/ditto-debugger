# M4 Results: First Real Experiment and Forced Kill-Switch Proof

**Date:** 2026-10-10 (Day 1)
**Target:** Eclipse Ditto 3.9.8, local Docker Compose (`deploy/ditto`)
**Approved by:** radhikakumar0705. The approvals were given in chat and recorded with `safety.cli approve --yes`. After review, `--yes` was removed: approvals now have to be typed (`APPROVE`) at an interactive terminal.
**How it was run:** [docs/m4-runbook.md](m4-runbook.md)

Raw run records are in `data/experiments/` (gitignored): `run-c8b30b07c9f3.json`, `run-d3f744a96f7a-forced-test.json` and `watchdog.jsonl`.

## Setup

- **Workload:** `simulator/fleet_workload.py` with 8 vehicles, writing, searching and polling freshness continuously.
- **Baselines** (`python -m safety.cli baseline`, built from live samples after more than 120 s of workload; 0% write errors in all three):

  | Baseline | Write samples | Write p95 |
  |---|---|---|
  | rehearsal | 238 | 15.96 ms |
  | run 1 | 682 | 15.73 ms |
  | run 2 | — | 17.48 ms |

- **Rehearsal:** both runs below were first rehearsed with `--dry-run`, which changes no container. Both rehearsals passed.

## Run 1: real experiment (`run-c8b30b07c9f3`)

`restart_container` on `things-search` for 10 s. The planner predicted `[things-search]`, meaning the search path.

| | |
|---|---|
| Status | **completed** |
| Kill switch | polled 5× during the fault; no breach (write error rate 0%, write p95 ≤ 19.6 ms against a 47 ms limit) |
| Rollback | `docker compose start things-search`; cleanup complete; watchdog disarmed |
| Recovery | **verified 4.2 s after rollback**: `/health` 200, authenticated search canary 200, SLIs since rollback within thresholds |
| Search outage (raw samples) | about **21 s**: requests failed from about 10:02:11 (the restart began) until 10:02:32. The last failure was 2.0 s after rollback; every search after it succeeded (16–57 ms) |
| Writes / freshness | write error rate 0% throughout (writes do not depend on things-search) |

**Observed behaviour (candidate finding for M5):** while things-search was down, search requests **hung until the client's 10 s timeout** (`ReadTimeout`) instead of failing fast with an error. A client without its own timeout would hang. Recommendation: a bounded search timeout or circuit breaker at the gateway or client, so search outages fail fast.

**Measurement note (not a Ditto weakness):** the workload runs its probes one after another, so each hung search blocked the next write for 10 s. Per-window write counts therefore dropped (56 → 24), and freshness p95 rose to about 10 s just after recovery. That comes from the client loop, not from Ditto. P3: run probes concurrently or with a shorter timeout, so an outage doesn't reduce the number of samples.

## Run 2: forced kill-switch proof (`run-d3f744a96f7a`)

`restart_container` on `things-search` for a planned 10 s, with `--forced-test`. The write-p95 limit is lowered to 0.01× baseline (0.17 ms) **during the fault only**.

| | |
|---|---|
| Status | **aborted**: `forced_test_passed: true` |
| Abort reason | `p95_latency_above_baseline_multiplier` (real write p95 21.2 ms > 0.17 ms limit) |
| Injected → abort | **2.5 s** into a planned 10 s fault (first kill-switch poll) |
| Abort → rollback | **0.5 s** (the duration of `docker compose start`) |
| Recovery | verified 9.0 s after rollback, using the normal thresholds |
| Score | `not_scored_forced_test`: never used as resilience evidence |
| Watchdog | armed at 10:03:12 before the fault; disarmed at 10:03:19 after a clean rollback |

Audit trail: `safety_preflight_started → fault_started → experiment_aborted → cleanup_complete → recovery_checked(verified)`.

## Fixed after these runs

`injected_at` used to be recorded *after* `docker compose restart` returned (about 8 s after the stop began). The runner also did not mark a fault as injected if the action command failed half-way. In that case only the watchdog, not the runner, would have restarted the target. Both are fixed in the same PR: the fault is marked injected and timestamped **before** the command runs, and there is a test for the half-failed command. Recovery times above are measured from rollback and are unaffected.

## After the runs: a test collision (not a Ditto issue)

At 10:03:53, *after* both runs had finished, a post-run `smoke_test.py` replaced the twin `bosch.fleet:vehicle-01`, which is also a fleet vehicle, with a battery-only twin. That made every later fleet write to it return 404 (about 14 per minute until the workload ended at 10:15). The workload's end-of-run summary therefore reports 7.7% write errors and an inflated freshness value. **Neither run is affected:** both finished by 10:03:28, and their records show 0% write errors.

The workload's summary also computes staleness against the current time for failed freshness reads that have no measured value, so those values keep growing. It reported 255 s, while the largest measured staleness was 10 s. That needs fixing on the P3 side (`monitor/sli.py`).

**Fix:** the smoke test now uses its own twin `bosch.fleet:smoke-test`, and a test enforces that it never uses a fleet vehicle ID.

## M4 checklist (CLAUDE.md)

- [x] One short, approved, isolated experiment on `things-search`, with rollback and a max duration
- [x] Forced abort proving the kill switch: it fired during the fault and rolled back automatically
- [x] Start, stop, rollback and recovery times recorded; result JSON written to `data/experiments/`
- [x] Independent watchdog armed before every fault
- [x] Stack healthy afterwards (`health_check.sh`: HEALTHY; `smoke_test.py`: 5/5)
