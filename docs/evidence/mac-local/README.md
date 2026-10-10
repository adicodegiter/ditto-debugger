# Mac-local run records (supplementary evidence)

Real run records from a second machine (a MacBook Air, Docker Desktop, the same pinned Ditto 3.9.8 stack).
`data/experiments/` is git-ignored, so these two files are copied here so other people and tools can use them.

They are **not** the team's official 5.5 records: those are in [`docs/m5-results.md`](../../m5-results.md) and were
measured on the Windows host. Do not mix the numbers. This Mac is slower (baseline write p95 70–100 ms against about 20 ms),
and the load generator, the workload and Ditto share one laptop's CPU.

| Run | Experiment | Baseline | Outcome |
|---|---|---|---|
| `run-c71e4e46b0e5` | `pause_container` on `things-search`, 30 s | `baseline-84f20b2e6d49`: 251 writes, 0% errors, p95 102.6 ms | Completed. Search error rate reached 100%, writes 0% errors, write p95 up to 149.7 ms. Recovery verified 20.8 s after unpause. Score 61.39. |
| `run-7d99f5e69cf8` | `traffic_burst` via `nginx`, 45 s | `baseline-83470ef78f71`: 536 writes, 0% errors, p95 100.6 ms | **Aborted by the kill switch** after 30.5 s: write p95 455 ms against a 302 ms limit (`p95_latency_above_baseline_multiplier`). No errors on any path. Recovery verified 4.4 s after rollback. Score 76.43 (detection 0.5 because it aborted). |

Both were rehearsed with `--dry-run` and approved by a human before running. Dry-run records are not shared: they are not resilience evidence.
These records pre-date the `score_inputs` field, so they carry `score_components` only.

## Use them

```bash
python -m ai.ranking --markdown \
  --run-file docs/evidence/mac-local/run-c71e4e46b0e5.json \
  --run-file docs/evidence/mac-local/run-7d99f5e69cf8.json
```

`--run-file` regenerates the findings deterministically from the record and does not need the local database.
