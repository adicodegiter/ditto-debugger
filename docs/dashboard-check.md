# Dashboard check on the live stack (task 6.4)

Checked 2026-10-10/11 against the running Ditto 3.9.8 stack, with the API served from this branch on `127.0.0.1:8000`.
Method: every route the dashboard calls was requested and its payload compared with what the view needs; `frontend/app.js` and
`frontend/index.html` were scanned for hardcoded data; and all five views were rendered in headless Chrome (1280 px wide) and
read as screenshots.

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

## Seen in the rendered pages

- Overview: planner agent "Ready", latest baseline and latest run (aborted burst) from the store, SLI tiles "No samples" with the
  workload stopped, header chip "Local - no live samples".
- Architecture: 13 components / 30 edges with provenance styles; the SLI request-path table and service table match the manifests.
- Experiments: 5 baselines with available/stale flags, 23 proposals with predictions, and the burst approval shown as expired.
- Runs: both stored runs; the burst run shows the abort reason, timeline, kill-switch charts, recovery check and findings.
- Safety: policy, thresholds, watchdog log and the 26-event audit trail.

## Not covered

- The live SLI panel needs the fleet workload running to show `live: true` (it was stopped during the check).
- Findings are stored when a run is recorded, so runs recorded before the kill-switch-abort fix keep their old findings.
- The runs in the database are from the Mac; the team's Windows-host runs are in `docs/m5-results.md`.

## Agent graph panel and a live run (2026-10-11)

Checked on the Windows host with the API on `127.0.0.1:8001` (LangGraph 1.2.12 installed) and the fleet workload
running:

- **Experiments → Agent graph:** with a workload run under 120 s old, **Run agent graph** stopped at `measure`
  after two attempts with the baseline refusal reason (this check predates the reuse-then-capture split of the two
  attempts); with a fresh stored baseline it went discover → measure →
  plan and showed "Paused before approval". **Resume at approval** showed each candidate as `pending` with "record a
  typed approval first". No console errors; `POST …/execute` still returned 503; no horizontal scroll at 375 px.
- **Runs:** after a typed approval in the dashboard, the operator CLI ran a 10 s `things-search` restart
  (`run-9285ae7dbc92`). The Runs view showed the timeline (inject 0 s, rollback +11 s, recovered +20 s), kill-switch
  charts, the recovery check (verified 9.3 s), where the fault landed, the findings and score 93.16.
