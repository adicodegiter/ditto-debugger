# API Contracts

The workbench API is FastAPI (`backend/app/main.py`), served on loopback only:
`python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000`. The live, generated
schema is at `http://127.0.0.1:8000/openapi.json`. JSON Schemas for the shared records (service, graph,
experiment, plan, run, observation, diagnosis, finding) are in [schemas/](../schemas/), with examples in
`schemas/examples/`; [schemas/openapi.yaml](../schemas/openapi.yaml) is the original contract for the core
routes. Records carry `schema_version` (`1.0`); a breaking change needs a new version and a migration.

**The API never executes a fault.** `POST /api/v1/experiments/{id}/execute` always returns `503`. Approved
experiments run only from the operator CLI (`python -m safety.cli execute <id>`), a separate process that
needs `CHAOS_EXECUTION_ENABLED=true` and a typed human approval.

## Endpoints

| Method and path | Purpose | Side effect / errors |
|---|---|---|
| `GET /api/v1/health` | Workbench process state, `execution_enabled`, agent status | Read only. Does not imply Ditto or Prometheus is reachable |
| `GET /api/v1/agents` | Agent status (discovery, mapping, telemetry, planning) | Read only |
| `GET /api/v1/architecture` | Current versioned graph from the manifests | Read only; `422` if a manifest cannot be parsed |
| `POST /api/v1/discovery/refresh` | Re-run discovery and mapping | Reads manifests only; `422` on a parse error |
| `GET /api/v1/telemetry` | Prometheus observations and coverage (optional) | Read only |
| `GET /api/v1/dependency-map` | `ai/dependency_map.json` (nodes, edges with provenance, SLI tags) | Read only |
| `GET /api/v1/slis?window=30` | Live SLIs over the last N seconds (5–600) from `data/metrics/*.jsonl` | Read only; `live: false` when no recent samples |
| `GET /api/v1/slis/series?minutes=5&bucket=10` | Bucketed SLI series for charts | Read only |
| `GET /api/v1/baselines?limit=20` | Stored baselines | Read only |
| `POST /api/v1/baselines` | Capture a baseline from the latest live workload run | Writes a baseline; `503` with the reason if refused (under 120 s of samples, stale run, unhealthy or missing SLI) |
| `POST /api/v1/experiments/plan` | Rank candidates for a baseline (`{"baseline_id", "max_candidates": 1–10}`) | Writes a proposed plan (`201`); never runs a fault. `404` unknown baseline, `503` if a planning gate fails |
| `GET /api/v1/experiments?limit=50` | Stored plans with candidates and approval state | Read only |
| `GET /api/v1/experiments/{id}` | One candidate with `candidate_hash`, `approval_status`, `approval` | Read only; `404` |
| `GET /api/v1/experiments/{id}/status` | Approval status and `execution_enabled` | Read only; `404` |
| `POST /api/v1/experiments/{id}/approval` | Record a human decision (`decision`, `reason`, `candidate_hash`) | Writes an approval (`201`); does not execute. `404` unknown id, `409` hash mismatch or decision already recorded |
| `POST /api/v1/experiments/{id}/execute` | — | **Always `503`**: execution is CLI-only |
| `GET /api/v1/runs?limit=50`, `GET /api/v1/runs/{run_id}` | Run records (timeline, observations, recovery, diagnosis, score) | Read only; `404` |
| `GET /api/v1/findings?run_id=` | Findings, optionally for one run | Read only; `404` unknown run |
| `GET /api/v1/findings/ranked` | Findings ranked across runs (severity, then confidence) | Read only |
| `GET /api/v1/audit?limit=100` | Append-only audit events | Read only |
| `GET /api/v1/policy` | Summary of `configs/experiment-policy.yaml` | Read only |
| `GET /api/v1/watchdog?limit=50` | Watchdog log (`data/experiments/watchdog.jsonl`) | Read only |
| `GET /api/v1/agent-graph` | Agent graph structure (nodes, edges with gate conditions, `interrupt_before`), engine (`langgraph` or `sequential-fallback`) and the last planning state | Read only; never runs a node |
| `POST /api/v1/agent-graph/run` | Run discover → measure → plan and stop before approval (`{"max_candidates": 1–10, "baseline_id"?}`) | Same writes as `POST /baselines` + `/experiments/plan` (`201`); a failed gate ends with `gate` and `reason`; `baseline_note` names a requested or stored baseline that was rejected or replaced. Never executes |
| `POST /api/v1/agent-graph/resume` | Resume a paused run at the approval node | Reports each candidate's approval status and next CLI step; `409` if nothing is paused. Never executes |
| `GET /` and `/assets/*` | Dashboard (`frontend/`) | Static files |

## Examples

Plan from a baseline:

```http
POST /api/v1/experiments/plan
Content-Type: application/json

{"baseline_id": "baseline-96847667f2d2", "max_candidates": 3}
```

```json
{
  "plan_id": "plan-f25b7b62ee51",
  "ranking_method": "weighted_heuristic_v1",
  "candidates": [
    {
      "schema_version": "1.0",
      "id": "restart_container-things-search-5fd1a78b855b5a2d",
      "target_component": "things-search",
      "action": "restart_container",
      "duration_seconds": 10,
      "predicted_slis": {"write": "hold", "search": "fail", "freshness": "hold"},
      "risk_score": 0.3,
      "evidence_refs": ["graph:eb1685d17ee80fb3", "baseline:baseline-96847667f2d2"],
      "approval_required": true,
      "selection_score": 0.68,
      "reason": "things-search (Things-Search): 1 direct graph dependent(s): gateway; …"
    }
  ],
  "policy_rejections": [],
  "execution_enabled": false
}
```

The response also carries `hypothesis`, `predicted_impacts`, `information_gain` and `score_terms` per candidate.
`candidate_hash` and `approval_status` (`pending`, `approved`, `rejected`) are added by the store; read them with
`GET /api/v1/experiments/{id}` before approving.

Record a human approval. The dashboard and CLI put the approver's name in the reason as
`approved_by=<name>; <reason>`; the hash binds the approval to the immutable candidate. Approvals expire after
`approval.expires_after_minutes` (10) and are single use.

```http
POST /api/v1/experiments/restart_container-things-search-5fd1a78b855b5a2d/approval
Content-Type: application/json

{"decision": "approve", "reason": "approved_by=radhika; demo", "candidate_hash": "sha256:2ed6…"}
```

Run the agent graph (advisory):

```http
POST /api/v1/agent-graph/run
Content-Type: application/json

{"max_candidates": 3}
```

```json
{
  "engine": "langgraph",
  "trace": ["discover", "measure", "plan"],
  "gate": "awaiting_approval",
  "reason": "a human must approve one candidate",
  "baseline_id": "baseline-96847667f2d2",
  "plan_id": "plan-f25b7b62ee51",
  "interrupted_before": ["approval"]
}
```

## Security and compatibility

- Loopback only; never bind to `0.0.0.0`. There is no authentication, so add it before any non-loopback use.
- Requests are validated by Pydantic models. No route accepts a command line, image name, host, Compose file path or
  Docker API body.
- Approval and execution are separate; neither the agent graph nor the optional LLM can approve or execute.
- Telemetry windows and candidate counts are bounded by query/body limits.
