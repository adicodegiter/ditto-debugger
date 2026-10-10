# M5 results: three fault types on the live stack (task 5.5)

Run on 2026-10-10 against the pinned Ditto 3.9.8 stack on the team's Windows host, with the
bounded fleet workload running (8 vehicles, 2 req/s, SSE freshness). Each experiment had a
fresh baseline, a dry run, an approval typed by a human (Radhika Kumar, P1) and a real run
through `python -m safety.cli execute` with `CHAOS_EXECUTION_ENABLED=true`. Raw records are
in `data/experiments/` (not in git); every number below comes from them.

| # | Fault | Run | Baseline | Outcome |
|---|---|---|---|---|
| 1 | `restart_container` on `things-search`, 10 s | `run-c8b30b07c9f3` (M4, see `docs/m4-results.md`) | write p95 15.7 ms | Completed; recovery verified 4.2 s after rollback |
| 2 | `pause_container` on `things-search`, 30 s | `run-370897363c60` | `baseline-b1d2516da16b`: 279 writes, 0% errors, p95 23.3 ms | Completed; recovery verified 23.6 s after unpause |
| 3 | `traffic_burst` via `nginx`, 45 s, 10 req/s, 16 separate twins | `run-4083aa7e13a1` | `baseline-1d065d712202`: 576 writes, 0% errors, p95 19.8 ms | Completed; recovery verified 4.3 s after the burst ended |

None of the three tripped the kill switch; every rollback was clean (`cleanup_state: complete`)
and unrelated components stayed healthy in every window.

## 2. Pause `things-search` (30 s)

Paused 17:11:01, unpaused 17:11:33, recovered 17:11:57 (UTC).

| SLI | Predicted | Observed during fault (worst 30 s window) | Verdict |
|---|---|---|---|
| Write | hold | 0% errors, p95 23.6 ms | match |
| Search | fail | 88.9% errors; samples per window fell from 56 to 9 | match |
| Freshness | hold | p95 0.024 s | match |

- Search requests did not fail fast: each one waited for the 3 s client timeout, so the probe
  rate dropped as the pause went on.
- Recovery took **23.6 s** after unpause, against 4.2 s for the restart in run 1.
- Resilience score **70.39** (complete): availability 0.11, latency 0.98, recovery 0.87,
  containment 1.0, detection 1.0.

## 3. Traffic burst through `nginx` (45 s)

Burst 17:27:17 to 17:28:03, recovered 17:28:07 (UTC). The burst made 358 writes, 350 searches
and 364 freshness reads on `vehicle-101` to `vehicle-116` (all succeeded; about 8 writes/s),
recorded separately in `data/metrics-burst/`.

| SLI | Predicted | Observed during fault (worst 30 s window) | Verdict |
|---|---|---|---|
| Write | degrade | 0% errors, p95 32.5 ms (1.6x baseline; abort limit 3x) | better than predicted |
| Search | degrade | 0% errors | better than predicted |
| Freshness | degrade | p95 0.032 s | better than predicted |

- Write p95 rose from about 20 ms to 32.5 ms and came back to 15 ms after the burst.
- At this rate Ditto absorbed the extra load; the planner's "degrade" prediction was too
  pessimistic. A higher rate (within the policy's bounds) is needed to find the limit.
- Resilience score **91.68** (complete): availability 1.0, latency 0.61, recovery 0.98,
  containment 1.0, detection 1.0.

## Control-plane issue found and fixed

The first burst dry run (`run-3fd934841e22-dry-run`, 60 s) aborted with
`experiment_deadline_exceeded`. The planned duration equalled the policy's
`max_duration_seconds` (60), and the kill switch's last poll lands about 1 s after the planned
end, so every 60 s fault would have aborted. `traffic_burst`, `stop_container` and
`cpu_throttle` now default to 45 s (`configs/experiment-policy.yaml`); no limit was loosened.

## Notes for the findings report (5.6)

- Candidate finding: pausing `things-search` makes search requests hang until the client
  timeout instead of failing fast, and recovery after unpause is about 5x slower than after
  a restart.
- The planner's history did not include the M4 restart (recorded before diagnosis was wired
  in), so it still called `things-search` restart "never tested before". Runs 2 and 3 are now
  in the history as `no_failure_observed`.
- Dry runs (`run-77166bc8c61f-dry-run`, `run-ccdbe2668919-dry-run`) are not scored.
