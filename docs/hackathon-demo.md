# Demo runbook and video script (3 minutes or less)

**Claim to show:** the system chooses a safe, evidence-backed failure experiment, a human approves it, only the
deterministic control plane runs it with a kill switch and rollback, and the result becomes ranked findings with a
resilience score. Everything is local, synthetic and loopback-only.

## Preflight (before recording)

```bash
bash deploy/health_check.sh                                      # OVERALL: HEALTHY
python -m unittest discover -s tests                             # all OK
python simulator/fleet_workload.py --duration-seconds 1200       # own terminal; keep it running
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Wait **at least 120 s** after starting the workload (a baseline needs 120 s of samples from the current run).
Install `requirements-graph.txt` if you want the panel to show the `langgraph` engine; without it the same flow
runs as `sequential-fallback`.

## Script

| Time | Show | Say |
|---|---|---|
| 0:00–0:25 | **Overview**: live chip, SLI tiles, workflow pipeline | Ditto 3.9.8, a Bosch-originated digital-twin platform, under a simulated vehicle fleet. Three SLIs: write, search, and freshness measured on Ditto's SSE stream. |
| 0:25–0:45 | **System map**: follow a search request | Agent 1 built this graph from the Compose manifest; every edge has a source, nothing is inferred. |
| 0:45–1:15 | **Experiments → Agent graph → Run agent graph** | LangGraph chains the agents: discover, measure, plan. The gates refuse to plan on a stale or incomplete baseline. The graph pauses before approval and can't execute. Point at a ranked proposal: hypothesis, per-SLI prediction, risk. |
| 1:15–1:35 | **Approve…** on the `things-search` restart: name, reason, type `APPROVE` | Approval is hash-bound, single use, expires in 10 minutes. Approving never executes. |
| 1:35–2:05 | Terminal: `CHAOS_EXECUTION_ENABLED=true python -m safety.cli execute <id>` | Only the operator CLI executes. Watchdog armed, fault injected, kill switch polled every 2 s, rollback, recovery check. The API's execute endpoint returns 503. |
| 2:05–2:40 | **Runs**: timeline, kill-switch charts, recovery check, findings, score | Recovered in N s. Predicted vs observed per SLI; the finding is evidence-linked, with a fix. |
| 2:40–3:00 | `docs/findings-report.md` top weakness and overall score | Search doesn't fail fast while things-search is down; the fix and its re-run are in the report. |

Before recording, run `python -m safety.cli execute <id> --dry-run` once; it changes nothing and keeps the approval.
A **forced kill-switch test** (`--forced-test`) is a good alternative for the run segment: it shows a real abort
and rollback, and is labelled and never scored.

## Reference run (2026-10-11, Windows host)

Agent graph (`langgraph`) planned from `baseline-96847667f2d2` and paused at approval; after a typed approval the
CLI restarted `things-search` for 10 s (`run-9285ae7dbc92`): completed, no abort, recovered 9.3 s after rollback,
worst search errors 4.7 %, write p95 25 ms, score 93.16. The post-run graph found write and freshness worse than
predicted (predicted hold, observed degrade).

## Stop conditions

Do not run if the health check fails, the baseline is refused, or another experiment is active. If a rollback does
not recover the target, stop the workload and run
`docker compose --env-file deploy/image-pins.env -f deploy/ditto/docker-compose.yml up -d`, then re-check health
before anything else. Never present the lab page's simulation or a fixture as a live run.
