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

Every run was rehearsed with `--dry-run` and approved by a human (typed `APPROVE`) first.
Dry-run records are not included: they are not resilience evidence. The forced kill-switch
test from Day 1 (`run-d3f744a96f7a-forced-test`) is described in `docs/m4-results.md` and is never scored.

```bash
python -m ai.ranking --markdown \
  --run-file docs/evidence/windows-host/run-370897363c60.json \
  --run-file docs/evidence/windows-host/run-4083aa7e13a1.json \
  --run-file docs/evidence/windows-host/run-a09cb1bcbb7c.json
```
