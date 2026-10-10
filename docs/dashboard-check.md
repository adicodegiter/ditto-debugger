# Dashboard check on the live stack (task 6.4)

Checked 2026-10-10/11 against the running Ditto 3.9.8 stack, with the API served from this branch on `127.0.0.1:8000`.
Method: every route the dashboard calls was requested and its payload compared with what the view needs; `frontend/app.js` and
`frontend/index.html` were scanned for hardcoded data. The pages were not screenshotted: open `http://127.0.0.1:8000` and look at
the five views before the demo.

| View | Routes | What it shows | Real data? |
|---|---|---|---|
| Overview | `/health`, `/agents`, `/slis`, `/slis/series` | agent status, live SLIs and a 5-minute series from `data/metrics/*.jsonl` | Yes. SLIs report `live: false` when the workload is not running. |
| Architecture (topology) | `/dependency-map` | 13 nodes, 30 edges, each with provenance | Yes. The SLI request-path table is now derived from the graph (was hardcoded). |
| Experiments (proposals, approvals) | `/experiments`, `/baselines`, `/experiments/plan` | stored plans, candidates with approval state, baselines | Yes |
| Runs | `/runs`, `/findings?run_id=`, `/watchdog` | run records, score, kill-switch charts, findings | Yes |
| Safety | `/policy`, `/audit`, `/watchdog` | allowlist, thresholds, audit trail | Yes |

## Defects found and fixed

1. `GET /api/v1/findings/ranked` returned 500 (`rank_findings` was never imported). Fixed, with an API test.
2. The planner agent status was the fixed string `blocked_until_baseline`. It now follows the stored baselines. Test added.
3. The Architecture "SLI request paths" table was a hardcoded service list. It is now built from each service's `workbench_slis`
   label and the graph's edges; on the live graph it gives the same lists as before, now sourced from the manifests.
4. A run aborted by the kill switch produced only an `info` finding. It now gets a `high` kill-switch-abort finding
   (not for forced tests or dry runs). Test added.

## Not covered

- No rendered-page check (see above).
- The live SLI panel needs the fleet workload running to show `live: true`.
- The runs in the database are from the Mac; the team's Windows-host runs are in `docs/m5-results.md`.
