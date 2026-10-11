# Ditto Codebase Map

This map points to the **upstream target** at the verified release tag `3.9.8`; it does not describe our custom AI agents as Ditto services. Our integration expects a separately managed checkout at `.local/ditto-upstream` and must not patch it. Upstream references: [Eclipse Ditto repository](https://github.com/eclipse-ditto/ditto/tree/3.9.8), [architecture overview](https://eclipse.dev/ditto/architecture-overview.html), [deployment tree](https://github.com/eclipse-ditto/ditto/tree/3.9.8/deployment), [Docker Compose deployment](https://github.com/eclipse-ditto/ditto/tree/3.9.8/deployment/docker), and [benchmark tool](https://github.com/eclipse-ditto/ditto/tree/3.9.8/benchmark-tool).

## Upstream Target Source Map

| Ditto runtime service | Upstream source area | Responsibility / boundary |
| --- | --- | --- |
| Gateway | [`gateway/service/`](https://github.com/eclipse-ditto/ditto/tree/3.9.8/gateway/service) | External HTTP/WebSocket API; translates to internal signals, no entity persistence |
| Policies | [`policies/service/`](https://github.com/eclipse-ditto/ditto/tree/3.9.8/policies/service) | Policy persistence and authorization enforcement |
| Things | [`things/service/`](https://github.com/eclipse-ditto/ditto/tree/3.9.8/things/service) | Thing/Feature persistence and enforcement |
| Things-Search | [`thingsearch/service/`](https://github.com/eclipse-ditto/ditto/tree/3.9.8/thingsearch/service) | Change tracking, search index, search queries; upstream module is named `thingsearch` |
| Connectivity | [`connectivity/service/`](https://github.com/eclipse-ditto/ditto/tree/3.9.8/connectivity/service) | External broker/endpoint connections and protocol/payload mapping |

Shared protocol/model code also exists in service-specific `model/` modules and top-level `protocol/`; inspect these when interpreting signal/API boundaries. All five services join the Pekko cluster and exchange serialized signals over TCP. **This documented cluster relationship is not proof of a particular request traversing every service.** MongoDB is backing storage; the Compose example adds nginx, UI, Swagger UI, and MongoDB around the five runtime services.

## Deployment, Operations, and Load Assets

| Upstream path | Use in our integration | Safety note |
| --- | --- | --- |
| [`deployment/docker/docker-compose.yml`](https://github.com/eclipse-ditto/ditto/blob/3.9.8/deployment/docker/docker-compose.yml) | Read-only inventory of services, images, ports, healthchecks, `depends_on`, environment references, and network declarations | Do not modify upstream; Compose relationships remain static evidence |
| [`deployment/docker/README.md`](https://github.com/eclipse-ditto/ditto/blob/3.9.8/deployment/docker/README.md) | Startup, local resource requirement, sample auth and config guidance | Review/replace demo auth; stay local |
| [`deployment/operations/`](https://github.com/eclipse-ditto/ditto/tree/3.9.8/deployment/operations) | Official operations assets, including Grafana dashboards | Reuse only with attribution/license review |
| [`benchmark-tool/artillery-test/`](https://github.com/eclipse-ditto/ditto/tree/3.9.8/benchmark-tool/artillery-test) | Official TypeScript/Artillery target workload and result tooling | It can create/delete Things, connections, and topics; isolate a synthetic namespace and confirm cleanup |
| [`benchmark-tool/mmock/`](https://github.com/eclipse-ditto/ditto/tree/3.9.8/benchmark-tool/mmock) | MMock definitions used by benchmark paths | Supporting load infrastructure, not a Ditto application service |

The default per-service liveness check in upstream Compose targets `http://<container>:7626/alive`; Prometheus exposition is on port `9095`, path `/`. Those are container-network endpoints and are not published to the host by default. The local integration should read these files, optionally add a **project-owned loopback-only Compose override**, and probe the running containers only after the operator supplies the isolated local deployment.

## Our Custom Repository Map

| Custom code | Owns | Must not do |
| --- | --- | --- |
| `backend/app/agents/discovery.py` | Read-only Compose/Kubernetes parser; component inventory and source provenance | Execute Compose, run commands, or label static edges as observed |
| `backend/app/agents/mapping.py` | Versioned graph, documented cluster membership, criticality hints | Invent per-request edges from network co-membership |
| `backend/app/agents/telemetry.py` | Loopback Prometheus polling, normalization, staleness/coverage | Call remote scrape targets or interpret missing samples as healthy |
| `backend/app/agents/planning.py` | Fixed candidate generation, history-aware weighted ranking and safety filtering | Execute experiments or treat LLM text as policy |
| `backend/app/orchestrator.py` | Specialist-agent coordination and persistence boundary | Collapse target Ditto services and our agents into one topology |
| `backend/app/graph.py` | Optional LangGraph agent graph over the orchestrator methods; pauses before approval | Import or call anything that executes |
| `backend/app/main.py` | Loopback API and static UI | Enable fault execution (`execute` always returns `503`) |
| `simulator/`, `monitor/` | Fleet workload, write/search/SSE-freshness probes, SLI math | Report a healthy baseline when samples are missing |
| `safety/` | Policy, runner, kill switch, watchdog, Docker adapter, recovery, scoring, operator CLI | Run anything not in the policy's fixed action table |
| `ai/` | Dependency map, diagnosis, findings, ranking, report export, optional Groq wording | Execute anything or change ids, severity or evidence |
| `frontend/` | Dashboard: overview, system map, experiments and agent graph, runs, safety | Claim a live experiment occurred when the API/target is unavailable |
| `schemas/` and `configs/` | Versioned agent handoffs and deterministic policy | Permit arbitrary actions or non-local targets |

The API and dashboard never execute. Fault execution exists only in the separate operator process (`python -m safety.cli execute`), behind `CHAOS_EXECUTION_ENABLED=true` and a typed human approval.