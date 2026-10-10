# Written BEFORE the live runs. Do not edit after the runs. Results go in the findings report.

- Generated at: 2026-10-10T17:04:12Z
- Git HEAD: 30aaa045614017724935ecc1b85bb27293a633d2
- Planner invocation: `ExperimentPlanningAgent.plan(...)` in `backend/app/agents/planning.py`, reached via `AgentOrchestrator.plan_experiments()` in `backend/app/orchestrator.py` and the `safety.cli plan` command in `safety/cli.py`.
- Source of truth for the freeze: `configs/experiment-policy.yaml`, the discovery+mapping graph from `deploy/ditto/docker-compose.yml`, and the same fixture-style planner run used by `tests/test_p4_planner.py`.
- No saved local baseline was found in `data/metrics/` or `data/`; this freeze therefore used the repo’s fixture baseline pattern instead of a gitignored real baseline.

## Planner candidates currently proposed by the policy

### Container interruption (restart or stop)

| Action | Target | Duration | Policy limits | Predicted SLIs (write / search / freshness) | Score | Rationale from graph or policy | Status |
|---|---|---:|---|---|---:|---|---|
| `restart_container` | `things-search` | 10 s | `max_duration_seconds: 60`; `risk_score: 0.30`; target allowed by policy scope; default 10 s | `hold / fail / hold` | 0.68 | `things-search` is directly upstream of `gateway`; the graph impact path is `things-search -> gateway`; policy picks a low-blast-radius first experiment. | HYPOTHESIS |
| `stop_container` | `things-search` | 60 s | `max_duration_seconds: 60`; `risk_score: 0.30`; target allowed by policy scope; default 60 s | `hold / fail / hold` | 0.68 | Same graph dependency as the restart: `things-search` has a direct graph dependent (`gateway`); policy permits a bounded stop only if within the global cap. | HYPOTHESIS |

### Pause

| Action | Target | Duration | Policy limits | Predicted SLIs (write / search / freshness) | Score | Rationale from graph or policy | Status |
|---|---|---:|---|---|---:|---|---|
| `pause_container` | `things-search` | 30 s | `max_duration_seconds: 60`; `risk_score: 0.30`; pause default is 30 s; target allowed by policy scope | `hold / fail / hold` | 0.68 | `things-search` has direct graph dependency on `gateway`; the policy allows pause and the action cleanup is `unpause_target` with bounded duration. | HYPOTHESIS |

### CPU throttle

| Action | Target | Duration | Policy limits | Predicted SLIs (write / search / freshness) | Score | Rationale from graph or policy | Status |
|---|---|---:|---|---|---:|---|---|
| `cpu_throttle` | `things-search` | 60 s | `max_duration_seconds: 60`; `risk_score: 0.30`; policy explicitly allows `cpu_throttle` for `mongodb` and `things-search`; `cpus: 0.25` | `hold / degrade / hold` | 0.68 | The planner keeps the action on the same service path as the other `things-search` candidates; the policy rule is explicit and the target remains within the configured scope. | HYPOTHESIS |

### Burst

| Action | Target | Duration | Policy limits | Predicted SLIs (write / search / freshness) | Score | Rationale from graph or policy | Status |
|---|---|---:|---|---|---:|---|---|
| `traffic_burst` | `nginx` | 60 s | `max_duration_seconds: 60`; `risk_score: 0.15`; explicit action target is `nginx`; `rate_per_second: 10`, `vehicles: 16` | `degrade / degrade / degrade` | 0.60 | The policy explicitly allows `traffic_burst` only on `nginx`, which is the API entry point; `nginx` has no direct graph dependents in the dependency map, so the effect is framed as a front-door load test. | HYPOTHESIS |

## Policy compliance check

- Allowlisted targets: `gateway` and `things-search` are enabled in the main scope; `nginx` is also allowed only as an explicit target for `traffic_burst`.
- Denied targets: `mongodb` is in `denied_targets` and is never returned by the planner for the normal container faults.
- Duration limits: the global cap is `60` seconds; every candidate is clamped to a duration that does not exceed that ceiling.
- Distinct fault types available: four (`container interruption`, `pause`, `cpu_throttle`, `burst`). This is not fewer than three.

## Evidence basis used for the freeze

This snapshot is based on the deterministic planner logic and the policy, not on a run record. The plan is intentionally frozen before the live runs and is not to be edited after an execution.
