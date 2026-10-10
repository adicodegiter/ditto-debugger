# Safety Model

## Non-Negotiable Boundary

Operate only on an isolated local Ditto deployment that the team owns. No public, shared, customer, or production targets. The experimental runner is opt-in, starts disabled, and cannot be invoked by the planner or LLM directly. The upstream deployment and benchmark can mutate or delete data; use synthetic Things in a dedicated namespace and ensure no real data is present.

## Approval and Execution

1. Safety checks the candidate against the checked-out pinned Compose project, policy, current baseline, target allow-list, action, duration, and run concurrency.
2. The UI presents the exact target, action, expected impact, risk, start/stop time, abort criteria, cleanup, and evidence. The operator explicitly approves or rejects.
3. Approval records identity, timestamp, experiment ID, policy version, and expiry (10 minutes in the initial example). It is single-use and bound to an immutable candidate hash.
4. Execution requires a fresh approval, rechecks health and policy, and resolves the target through a hard-coded service-to-Compose mapping. It cannot run arbitrary strings or model-generated commands.
5. The runner stops the selected container for no more than 30 seconds and starts it again in a `finally` cleanup path. It records action and cleanup result. It then waits for `/alive` and a functional API canary; timeout is a failed recovery, not a successful experiment.

Approval is necessary but not sufficient: all deterministic checks still run at execution time. No API endpoint accepts direct Docker operations. Do not mount the Docker socket into a network-facing API or any model process. If the isolated runner requires Docker control, keep it as a separate local process with operator-managed access; document the full host-control implications.

## MVP Limits

- Allowed action: a short restart of `gateway` or `things-search` only, each must be reviewed at preflight. `mongodb` is explicitly denied. Restrict to one active experiment.
- Maximum fault duration: 30 seconds; default 10 seconds. No concurrent experiments or chained actions.
- Preconditions: known local Compose project; all baseline target/dependencies healthy; metrics and API canary working; workload at low fixed rate; no other run active; sufficient disk/memory; no unknown container identities.
- Stop/abort immediately on baseline loss, unrelated service failure, configured error-rate threshold, resource guard breach, deadline expiry, approval expiry, target mismatch, or lost telemetry. Cleanup always attempts to start the named target.
- Recovery: verify container running/healthy, `/alive` returns healthy for the service, and API canary succeeds. Record recovery time and any incomplete cleanup. If Docker/host failure prevents automatic recovery, require the operator to run the documented Compose `up -d` recovery command before continuing.
- Result data is synthetic. Before each run, verify test data namespace and cleanup plan. Preserve enough history for repeatable evaluation; do not purge arbitrary namespaces.

The local-only settings in [experiment-policy.yaml](../configs/experiment-policy.yaml) are enforced by the deterministic `safety/` package. Revalidate all thresholds after the first resource/health preflight. A safety violation blocks a score and triggers manual review.

## Scenario Catalog

| Scenario | Target and preconditions | Expected effect and observations | Duration / abort | Recovery / status |
| --- | --- | --- | --- | --- |
| Restart one Gateway container | Local `gateway`, healthy baseline, bounded HTTP traffic, human approval | API errors/latency while down; container health, HTTP success and latency after restart | 10 s default, 30 s absolute cap; abort on baseline/other component failure, rate/host limits, or telemetry loss | Start container in cleanup; `/alive` plus functional API canary. **MVP candidate** |
| Restart one Things-Search container | Same, plus search query canary/data prepared | Search requests degrade; inspect recovery and query behavior | Same strict cap/abort | Restart; health + search canary. **MVP after gateway path** |
| Ingress latency | Toxiproxy only on local client-to-nginx/API path; no peer traffic routed through it | API p95 and timeout change | Small fixed delay, short window; reset proxy on every exit | Remove toxic and rerun canary. **Later; validate proxy routing first** |
| Peer network partition / service-to-service latency | Would require controlled cluster network isolation | Pekko cluster behavior may affect multiple services | Not permitted in MVP; broad blast radius | Only after dedicated cluster, reviewed network policy, and independent recovery mechanism. **Deferred** |
| MongoDB stop/unavailability | Persistent storage | Could disrupt writes and recovery/data state | **Denied** | Not a hackathon MVP experiment |
| CPU/memory pressure | Any service | Resource exhaustion can affect host and all containers | **Denied** until hard limits and resource guard are verified | No generic stress tools or unbounded workloads |

## Model and API Protections

Treat Compose, telemetry, log content, model responses, and issue text as untrusted data. Parse constrained known Compose files rather than executing input. Escape/limit display fields. No LLM function calling, command generation, policy editing, network access, or secret access. Audit approval, safety decision, runner action, abort, recovery, and cleanup without logging credentials.