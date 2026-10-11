# Safety Model

## Non-Negotiable Boundary

Operate only on an isolated local Ditto deployment that the team owns. No public, shared, customer, or production targets. The experimental runner is opt-in, starts disabled, and cannot be invoked by the planner or LLM directly. The upstream deployment and benchmark can mutate or delete data; use synthetic Things in a dedicated namespace and ensure no real data is present.

## Approval and Execution

1. Safety checks the candidate against the checked-out pinned Compose project, policy, current baseline, target allow-list, action, duration, and run concurrency.
2. The UI presents the exact target, action, expected impact, risk, start/stop time, abort criteria, cleanup, and evidence. The operator explicitly approves or rejects.
3. Approval records identity, timestamp, experiment ID, policy version, and expiry (10 minutes in the initial example). It is single-use and bound to an immutable candidate hash.
4. Execution happens **only in the separate operator process**: `python -m safety.cli execute <experiment_id>`. It requires `CHAOS_EXECUTION_ENABLED=true`, a fresh single-use approval, and a stored baseline younger than `baseline_max_age_seconds`. The API's execute endpoint always returns 503, and no API or model process can run Docker. Actions map to a fixed command table in `safety/docker_adapter.py`: `restart_container` → `docker compose restart`, `stop_container` → `docker compose stop`, `pause_container` → `docker compose pause` (rollback `unpause`), `cpu_throttle` → `docker update --cpu-period/--cpu-quota` (rollback restores the captured quota), and `traffic_burst` → a bounded extra fleet workload through nginx that ends by itself (rollback terminates it). Every command is pinned to the project's compose file and `deploy/image-pins.env`. Preflight checks that the compose project name matches the policy, that the target is a service in it, and that the target is running.
5. **Steady state:** before any fault, the last 30 s of real SLI samples must pass the kill switch. If the stored baseline has no write p95, the pre-fault window provides it.
6. **Watchdog:** an independent, detached process is armed *before* injection. At `duration + grace_seconds` it runs `docker compose start <target>`, whether or not the runner is still alive. It is disarmed only after the runner's own cleanup succeeds; if that cleanup fails, the watchdog stays armed and retries.
7. **Live kill switch:** every `poll_interval_seconds` (2 s) during the fault, the runner evaluates the last `window_seconds` (30 s) of `data/metrics/*.jsonl`. The first abort rolls back immediately. Limits come only from the policy (`abort_thresholds`), never from the data:
   - write error rate > 20%
   - write p95 > 3× baseline p95
   - freshness p95 > 30 s
   - fewer than the minimum samples
   - any other service not running or not healthy
   - deadline exceeded
8. **Recovery** is checked after every run, including aborted ones. It requires nginx-routed `/health` = 200, an authenticated read-only API canary = 200, and the SLIs since rollback back within thresholds. Recovery time is measured from rollback. If it times out (`recovery.timeout_seconds`), the run is `failed` and needs operator recovery.

Dry run: `python -m safety.cli execute <id> --dry-run` performs every read (preflight, steady state, live kill switch, recovery) against the real stack. It records the stop and start commands instead of running them, and does not consume the approval.

Approval is necessary but not sufficient: all deterministic checks still run at execution time. No API endpoint accepts direct Docker operations. Do not mount the Docker socket into a network-facing API or any model process. If the isolated runner requires Docker control, keep it as a separate local process with operator-managed access; document the full host-control implications.

## Limits

- Allowed actions (policy `actions`): `restart_container`, `stop_container`, `pause_container` on `gateway` or `things-search`; `cpu_throttle` (0.25 CPU) on `things-search` or `mongodb`, the only action allowed on `mongodb`; `traffic_burst` (10 req/s, 16 separate twins) through `nginx`. Every action is reviewed at preflight. One active experiment at a time (`max_concurrent_experiments: 1`).
- Maximum fault duration: 60 seconds (`max_duration_seconds`). Defaults: restart 10 s, stop 45 s, pause 30 s, CPU throttle 45 s, burst 45 s, all below the cap because the last kill-switch poll lands about 1 s after the planned end.
- Combined failures (`compound` in the policy): at most two staged faults from `allowed_pairs`, stagger at most 20 s, one kill switch; each fault rolls back at its own end and an abort rolls back everything in reverse order. See [combined-failures.md](combined-failures.md).
- Preconditions: known local Compose project; all baseline target/dependencies healthy; metrics and API canary working; workload at low fixed rate; no other run active; sufficient disk/memory; no unknown container identities.
- Stop/abort immediately on baseline loss, unrelated service failure, configured error-rate threshold, resource guard breach, deadline expiry, approval expiry, target mismatch, or lost telemetry. Cleanup always attempts to start the named target.
- Recovery: verify nginx-routed `/health` (Ditto's per-service `/alive` is on internal port 7626 and not routed), an authenticated API canary, and SLIs back within thresholds. Record recovery time and any incomplete cleanup. If Docker/host failure prevents automatic recovery, require the operator to run the documented Compose `up -d` recovery command before continuing.
- Result data is synthetic. Before each run, verify test data namespace and cleanup plan. Preserve enough history for repeatable evaluation; do not purge arbitrary namespaces.

The local-only settings in [experiment-policy.yaml](../configs/experiment-policy.yaml) are enforced by the deterministic `safety/` package. Revalidate all thresholds after the first resource/health preflight. A safety violation blocks a score and triggers manual review.

## Scenario Catalog

| Scenario | Target and preconditions | Expected effect and observations | Duration / abort | Recovery / status |
| --- | --- | --- | --- | --- |
| Restart one Gateway container | Local `gateway`, healthy baseline, bounded HTTP traffic, human approval | API errors/latency while down; container health, HTTP success and latency after restart | 10 s default, 60 s absolute cap (policy `max_duration_seconds`); abort on baseline/other component failure, rate/host limits, or telemetry loss | Start container in cleanup (watchdog as backstop); `/health` plus authenticated API canary and SLIs back within thresholds. **MVP candidate** |
| Restart one Things-Search container | Same, plus search query canary/data prepared | Search requests degrade; inspect recovery and query behavior | Same strict cap/abort | Restart; health + search canary. **MVP after gateway path** |
| Ingress latency | Toxiproxy only on local client-to-nginx/API path; no peer traffic routed through it | API p95 and timeout change | Small fixed delay, short window; reset proxy on every exit | Remove toxic and rerun canary. **Later; validate proxy routing first** |
| Peer network partition / service-to-service latency | Would require controlled cluster network isolation | Pekko cluster behavior may affect multiple services | Not permitted in MVP; broad blast radius | Only after dedicated cluster, reviewed network policy, and independent recovery mechanism. **Deferred** |
| Pause Things-Search | `things-search`, healthy baseline, approval | Search hangs or fails while frozen; writes should hold | 30 s default | `docker compose unpause`; health + canary + SLIs. **Run live** (`docs/m5-results.md`) |
| Traffic burst | Extra bounded workload through nginx on separate twins (`vehicle-101`+) | Write/search latency rises under load | 45 s default, auto-stops at its cap | Terminate the burst process. **Run live** |
| CPU throttle | `docker update` CPU quota on `things-search` or `mongodb` | Slower search/persistence | 45 s default | Restore the captured quota. **Enabled; not part of the recorded runs** |
| MongoDB stop/unavailability | Persistent storage | Could disrupt writes and recovery/data state | **Denied** (only a CPU throttle may target MongoDB) | Not a hackathon experiment |
| Memory pressure / generic stress | Any service | Resource exhaustion can affect host and all containers | **Denied** | No generic stress tools or unbounded workloads |

## Agents, Agent Graph, Model and API Protections

The optional LangGraph agent graph (`backend/app/graph.py`) only chains the advisory agents. It is compiled with an interrupt before the human-approval node and ends there; resuming only reports approval status. The module imports nothing from the runner, Docker adapter, kill switch, watchdog or CLI, and `tests/test_agent_graph.py` checks its imports, its code and each node callable for any execution path.


Treat Compose, telemetry, log content, model responses, and issue text as untrusted data. Parse constrained known Compose files rather than executing input. Escape/limit display fields. No LLM function calling, command generation, policy editing, network access, or secret access. Audit approval, safety decision, runner action, abort, recovery, and cleanup without logging credentials.