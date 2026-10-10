# Windows-host run records (official 5.5 / 5.7 evidence)

Real run records from the team's Windows host (Docker Desktop + WSL2, pinned Ditto 3.9.8).
`data/experiments/` is git-ignored, so these files are copied here so the findings report,
score and video can cite them. They are the records behind [`docs/m5-results.md`](../../m5-results.md)
and [`docs/m4-results.md`](../../m4-results.md). Do not mix them with the Mac records in
[`../mac-local/`](../mac-local/README.md): that machine is about 5x slower.

| Run | Experiment | Baseline write p95 | Outcome |
|---|---|---|---|
| `run-c8b30b07c9f3` | `restart_container` on `things-search`, 10 s (M4, Day 1: 10 s search timeout, serial probes) | 15.7 ms | Completed; recovery verified 4.2 s after rollback |
| `run-370897363c60` | `pause_container` on `things-search`, 30 s (5.5) | 23.3 ms | Completed; search errors up to 88.9%; recovery verified 23.6 s after unpause; score 70.39 |
| `run-4083aa7e13a1` | `traffic_burst` via `nginx`, 45 s (5.5) | 19.8 ms | Completed; write p95 up to 32.5 ms, no errors; recovery verified 4.3 s; score 91.68 |
| `run-a09cb1bcbb7c` | `restart_container` on `things-search`, 10 s (5.7 re-run: 3 s search timeout, concurrent probes) | 24.7 ms | Completed; search errors up to 4.8%; recovery verified 22.4 s after rollback; score 89.0 |

Also here: `run-d3f744a96f7a-forced-test.json`, the Day 1 forced kill-switch test (aborted 2.5 s
into the fault on purpose; described in `docs/m4-results.md`). It is labelled and never scored or ranked.

Every run was rehearsed with `--dry-run` and approved by a human (typed `APPROVE`) first.
Dry-run records are not included: they are not resilience evidence.

## Probe samples (`samples/`)

`samples/<run_id>.jsonl` holds the raw write and search probe samples from 5 s before injection to
5 s after recovery, cut from the git-ignored `data/metrics/*.jsonl` with:

```bash
python -m ai.request_evidence --run docs/evidence/windows-host/<run_id>.json
```

They carry the request-level facts the run records cannot: how long a failing search hung, how long
search was out, and the write probe rate during the outage. Fields: kind, timestamp (request end),
latency, success, error type, vehicle ID. No credentials or personal data.

## Findings report

`docs/findings-report.md` is generated from this folder:

```bash
python -m ai.report_export
python -m ai.trace_check docs/findings-report.md
```

Ranked findings only:

```bash
python -m ai.ranking --markdown \
  --run-file docs/evidence/windows-host/run-370897363c60.json \
  --run-file docs/evidence/windows-host/run-4083aa7e13a1.json \
  --run-file docs/evidence/windows-host/run-a09cb1bcbb7c.json
```
