# Project: Ditto Resilience Workbench
AI-driven chaos engineering for a connected-vehicle digital-twin backend (Bosch BGSW hackathon, 2 days).

## What we are building
One coordinated system that maps a locally running Eclipse Ditto deployment, measures its
healthy behaviour under a simulated vehicle fleet, plans safe failure experiments, runs them
through a deterministic control plane with a kill switch, and produces an evidence-linked,
ranked findings report with fixes and a resilience score.

- **Application under test:** Eclipse Ditto **3.9.8** (pinned in `deploy/image-pins.env`),
  Docker Compose, on the team's Windows host (Docker Desktop + WSL2).
- **Pitch line:** AI-driven chaos engineering for a connected-vehicle digital-twin backend,
  built on Eclipse Ditto (a Bosch-originated platform).

## Hard rules (from the hackathon brief; never break these)
- Everything runs ONLY locally. `DITTO_BASE_URL` defaults to localhost; no public
  sandboxes, cloud services or external endpoints in the app under test.
- Never affect anything outside our own Ditto containers: not the host network, the Docker
  daemon, other machines, other teams, or shared infrastructure.
- Agents never execute anything. Only the deterministic control plane executes, and only
  predefined actions on allowlisted targets from a human-approved, immutable plan.
- Every experiment MUST have: rollback, max duration, abort thresholds, recovery check.
- The API `execute` endpoint returns 503 by design. Keep it that way until the runner,
  rollback and kill switch are wired and the forbidden-action tests pass (M3 gate).
- Never invent services or edges without a manifest source. Never report a healthy
  baseline when samples or metrics are missing.
- Pin every image; never `latest`. No secrets in code (read from env / `.env`).
- Every LLM feature has a working no-API-key fallback; LLM output is schema-checked.
- Ask before destructive commands (docker system prune, volume deletion, rm -rf, anything
  outside this repo). No `git push` without the user's confirmation.

## Architecture: three agents over a deterministic control plane

| Agent | Built from | Owns | Must not do |
|---|---|---|---|
| 1. Topology & Evidence | Existing discovery + mapping code | `backend/app/agents/discovery.py`, `backend/app/agents/mapping.py`, `ai/dependency_map.json` | Invent services or edges with no manifest source |
| 2. Workload & Measurement | Three SLIs, JSON Lines rule, bounded workload | `simulator/`, `monitor/`, `data/metrics/` | Report a healthy baseline when samples are missing |
| 3. Planner & Analyst (AI) | Existing planner, history, report requirement | `backend/app/agents/planning.py`, `ai/` report code | Execute anything; output must be schema-checked and evidence-linked |

**Control plane (deterministic, NOT an agent):** `safety/` (runner, kill switch, rollback,
recovery check), `configs/experiment-policy.yaml` (allowlist, fault catalog, thresholds),
`deploy/` (pinned Compose, image pins, health check, reset).

**Interaction flow**
1. Agent 1 produces a versioned graph and passes its graph ID to Agent 2.
2. Agent 2 returns a baseline and samples for that graph.
3. Agent 3 ranks candidate experiments (gated on graph, telemetry coverage and baseline
   freshness) with predicted impact and a reason.
4. A human approves one plan; it becomes immutable.
5. The control plane runs it with the kill switch, max duration and rollback.
6. Agent 2 measures impact and recovery.
7. Agent 3 compares predicted vs observed and writes the findings report.

Ditto services: nginx → gateway → policies / things / things-search / connectivity → MongoDB.

## SLIs (Agent 2)
1. **Write path:** success rate + p50/p95 latency of telemetry writes — built
2. **Search path:** success rate + latency of fleet search queries — built
3. **Freshness:** staleness of each telemetry change when it arrives on Ditto's SSE stream
   (`received_at − last_seen`). `--freshness-method poll-proxy` is the labelled fallback.
**Recovery time** = rollback until all SLIs are back within baseline.
All samples are JSON Lines in `data/metrics/*.jsonl`, ISO-8601 UTC timestamps.

## Kill switch thresholds (wired to the runner; polled every 2 s during a fault)
| Signal | Abort when | Window |
|---|---|---|
| Write error rate | > 20% | last 30 s |
| Write p95 latency | > 3x baseline p95 | last 30 s |
| Freshness p95 staleness | > 30 s | last 30 s |
| Missing samples | treated as unhealthy, never as healthy | last 30 s |
| Any experiment | exceeds `max_duration_s` | — |

## Allowed fault types (predefined control-plane actions only)
Done: container restart/stop of `things-search` (M4). Day 2 (merged in #12, run live in 5.5):
`pause_container` (compose pause/unpause), `cpu_throttle` (`docker update --cpu-quota`, the
only action that may target `mongodb`), `traffic_burst` (bounded extra workload against
nginx on separate twins, auto-stops at its cap). Defaults stay below the 60 s cap (45 s; the
last kill-switch poll lands ~1 s after the planned end). MongoDB stays out of routine fault injection. Later, if time
allows: network delay inside a target container's namespace, capped disk fill in our MongoDB
volume (rollback deletes it). Combined failures are a Day-2 stretch goal.

## Repo layout
```
backend/app/
  main.py, settings.py
  agents/discovery.py, mapping.py, planning.py     # Agents 1 and 3
simulator/          # bootstrap + fleet workload (Agent 2)
monitor/            # write, search, freshness probes; SLI math (Agent 2)
safety/             # runner, kill switch, watchdog, rollback, recovery, operator CLI (control plane)
ai/                 # dependency_map.json, diagnosis + report code
configs/experiment-policy.yaml
deploy/             # image-pins.env, compose file, health_check, reset
schemas/            # versioned shared contracts
frontend/           # dashboard
docs/               # architecture overview, findings report
tests/
data/               # runtime metrics + experiment records (gitignored)
TOOLS.md            # tools, libraries, AI models (for the team form)
```

## Team ownership (5 people, 3 agents + control plane)
| Person | Owns |
|---|---|
| P1 | Ditto deploy (`deploy/`), discovery (Agent 1), integration + merges |
| P2 | Mapping / graph (Agent 1), schemas, API, dashboard, architecture page |
| P3 | Workload + measurement (Agent 2), SSE freshness, baseline, demo video |
| P4 | Planner + analyst (Agent 3), diagnosis, findings report |
| P5 | Control plane: policy, runner, kill-switch wiring, rollback, resilience score |

## Current status (`main`, 2026-10-11; tasks in `docs/day2-plan.md`)
| Item | Status |
|---|---|
| M0: pinned Ditto 3.9.8 (`deploy/image-pins.env`, `deploy/ditto/`), loopback-only ports | Done; `health_check.sh` and `smoke_test.py` (own twin `bosch.fleet:smoke-test`) pass |
| M1: discovery + graph + `ai/dependency_map.json` + `docs/dependency-map.md` | Done (`python -m ai.dependency_map`); every edge has a source |
| M2: workload, three SLIs, 120 s run-scoped baseline, SSE freshness, concurrent probes | Done (PR #8) |
| M3: safety gate | Done: live kill switch (2 s polls, policy-only limits), watchdog, fixed actions, typed approvals, API `execute` always 503, dry run |
| M4: real `things-search` restart + forced kill-switch abort | Done; `docs/m4-results.md` |
| M5: fault types, predicted vs observed, ranked findings | Done. Restart, pause and burst run live with recovery verified (`docs/m5-results.md`, records in `docs/evidence/windows-host/`); per-SLI predictions and diagnosis on real runs; ranked findings (`python -m ai.ranking`); 5.7 re-run: a failing search now hangs 3.0 s instead of 10.0 s |
| M6: score, deliverables | Score done (`score_components` + `score_inputs`); dashboard checked live (`docs/dashboard-check.md`); architecture page rewritten. **Pending: findings report `docs/findings-report.md` (P4), demo video (P3), team form submission (P1)** |
| Agent 3 LLM | Optional Groq adapter (`ai/llm.py`, `ai/explain.py`), off unless `AI_PROVIDER=groq`; deterministic findings are the fallback |
| Tests | 148 passing on `main` (`python -m unittest discover -s tests`) |

**How to run an experiment:** `docs/m4-runbook.md` (`safety.cli baseline → plan → approve → execute [--dry-run|--forced-test]`).
Only the operator CLI executes; it needs `CHAOS_EXECUTION_ENABLED=true` and an approval typed by a human.

**Verified on the live 3.9.8 stack:** the Ditto v2 API paths (`/api/2/policies`, `/api/2/things`,
`/api/2/search/things`), the policy body with the `nginx:ditto` subject, and the `mongo:7.0.14` tag.
Use `127.0.0.1` rather than `localhost` in `DITTO_BASE_URL`: on Windows `localhost` tries IPv6 `::1`
first (+2 s per connection).

## Milestones
| Milestone | Content | When |
|---|---|---|
| M0 | Add pinned Compose file; stack healthy on the Windows host; run README commands; verify API paths, policy subject, Mongo tag; install `fastapi`; all tests green | Day 1 morning |
| M1 | Discovery + graph from the real manifests; `dependency_map.json` | Day 1 midday |
| M2 | Bootstrap + workload live; baseline (120 s minimum, 10 min preferred) with full coverage; SSE freshness replaces the poll proxy | Day 1 afternoon |
| M3 | Kill switch wired to the runner in dry-run; rollback implemented; forbidden-action tests pass; then lift the 503 | Day 1 late afternoon |
| M4 | One real `things-search` restart/stop with rollback and max duration, plus a forced abort proving the kill switch | **Day 1 evening (brief's Day-1 minimum)** |
| M5 | 3+ fault types; predicted vs observed; ranked findings with fixes (tasks 5.1–5.7 in `docs/day2-plan.md`) | Day 2 morning |
| M6 | Resilience score, findings report, README Day 2 steps, architecture page, video, team form, submit 30 min early (tasks 6.1–6.8) | Day 2 afternoon |

**Day 2 critical path:** 5.1 (pause), 5.2 (burst), 5.3 (diagnosis on real runs) in parallel →
5.4 (per-SLI predictions) → **5.5 three live runs** (restart, pause, burst; each with fresh
baseline, dry run, typed approval) → findings 5.6 + remediation re-run 5.7 + score 6.1 →
report 6.2 → README 6.5 / video 6.6 → final merge 6.8. 6.3 (architecture page) and 6.7 (team
form, draft in `docs/team-form.md`) have no dependencies. P1 owns 5.3 merge, 6.5, 6.7, 6.8 and keeps the stack healthy
(`deploy/health_check.sh` before each run).

If the stack is not healthy with the workload running by 13:00 on Day 1, fall back to
OWASP crAPI (only deployment, workload and service names change).

## Working conventions
- Windows host: run shell scripts via WSL2 or Git Bash, or provide PowerShell equivalents.
- Python 3.11+; requirements must include `fastapi` and everything the tests import.
- One branch per person; PRs into `main`; P1 merges at checkpoints.
- Small, working increments; verify against the live stack before claiming "done".
- Tests for: SLI math, missing samples, kill-switch thresholds, malformed manifests,
  schema validation, and forbidden targets/actions being rejected.
- Every script has `--help`; every metric goes to `data/metrics/*.jsonl`.
- Keep `README.md` and `TOOLS.md` current; both feed graded deliverables.

## Final deliverables (Day 2)
1. Source code: public repo + README (setup and run)
2. Demo video (3 min or less): failure injected, impact observed, findings reported
3. Architecture overview (1 page): components, how the AI decides, safety controls
4. Findings report: weaknesses ranked by severity, recommended fixes, resilience score
5. Team form: members, tools and AI models, third-party libraries, data used