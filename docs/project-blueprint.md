# Project Blueprint

**Status:** Day-1 blueprint, kept for history. The system is now built; current state is in [README.md](../README.md), [CLAUDE.md](../CLAUDE.md) and [architecture.md](architecture.md). Verified upstream facts in this document were checked against Eclipse Ditto's official repository and documentation on 2026-10-10. Upstream `master` and online development documentation move; use the pinned release and verify before implementation.

## Problem and Users

Resilience engineers need to know whether an IoT digital-twin deployment remains useful during component failures, how quickly it detects and recovers, and which dependency causes the observed impact. Random fault injection can demonstrate breakage, but does not explain why a particular experiment is worth running or what was learned.

Primary users are hackathon engineers operating the isolated test stack and reviewers evaluating evidence. Inputs are a pinned Ditto Compose deployment, declared/documented topology, Prometheus-compatible metrics, a bounded synthetic workload, policy limits, and past experiment records. Outputs are a confidence-labelled component graph, ranked proposed experiments with predictions, approval/audit records, observations, reproducible resilience metrics, findings with evidence references, and suggested mitigations.

Assumptions: one local Ditto Compose installation; synthetic data; one operator; no production use; human approval for every fault; an LLM is optional. Constraints: laptop-scale resources, short build time, no required paid cloud or external model, and no changes to Ditto's source tree.

## What Makes It AI-Assisted

| Approach | Selection basis | Limitation |
| --- | --- | --- |
| Random fault injection | Random component/action | Can be low-value, unsafe, and hard to interpret |
| Rule-based selection | Fixed if/then rules and a static dependency list | Repeatable, but does not adapt priorities to evidence/history |
| This system | Graph coverage, measured baseline, uncertainty, impact, safety policy, and prior outcomes rank a bounded candidate set; optional LLM explains evidence and proposes hypotheses | The initial planner is a transparent heuristic, not autonomous learning or a reliability guarantee |

The MVP proves AI-assisted decision-making when the planner selects a previously untested, high-value edge/action over a repeatedly tested low-information candidate, explains the evidence and uncertainty, and updates its ranking after the result. Merely sending logs to an LLM for a summary does not meet this bar.

## Scope

**MVP:** read the pinned Compose manifest and verified Ditto service facts; build a provenance/confidence-labelled graph; collect health, API canary, load results, and documented Prometheus metrics; establish a baseline; rank a fixed candidate set deterministically; require approval; run one short, allow-listed local service restart; verify recovery; persist the prediction/result; calculate transparent metrics; produce an evidence-linked report. The LLM may explain only supplied evidence; deterministic report templates work without it.

**Later:** better runtime discovery and trace correlation; ingress latency through a local proxy; multiple replicas/Kubernetes; historical Bayesian calibration; richer dashboard; security/dependency analysis; additional failure modes after safety review. No arbitrary shell execution, production integration, autonomous execution, or broad resource exhaustion is in scope.

## Components and Boundaries

See [architecture.md](architecture.md), [safety-model.md](safety-model.md), and [api-contracts.md](api-contracts.md). The adapter is read-only for discovery/telemetry. The planner emits typed candidates but cannot execute. The safety controller alone applies deterministic policy. A separate fixed-action runner executes only after a recorded human approval. Evaluation consumes normalized observations and records data; analysis is evidence-constrained. SQLite stores small control-plane records; Prometheus stores metric history. The dashboard calls only the versioned local API.

## Technology Decisions

| Concern | MVP choice | Rationale / deferred alternative |
| --- | --- | --- |
| Target | Eclipse Ditto release `3.9.8` | Verified five-service IoT framework, local Compose, native metrics, Bosch IoT relevance. Recheck release at preflight. |
| Orchestration API | Python 3.12+, FastAPI, Pydantic | Fast to test and suitable for typed API and local adapters. No app framework is installed yet. |
| UI | React + TypeScript, Vite | Thin approval/graph/result interface after the data flow is stable; do not block the first experiment on a UI. |
| Telemetry | Ditto `/metrics` + Prometheus | Ditto exports Prometheus-format metrics; Prometheus supplies queryable history. Use a loopback-only Compose override for scrape access. |
| Visualization | Custom thin dashboard; reuse upstream Grafana dashboard JSON only if useful and licensing/attribution are preserved | Avoid deploying a second dashboard stack for the first demonstration. |
| Load | Official Ditto Artillery test, bounded HTTP scenario first | Avoid inventing workload semantics. Its documented prerequisites include Node.js, Ditto, Kafka with topic deletion enabled, and MMock; verify if the selected HTTP-only setup can omit extras. |
| Fault execution | Fixed local Docker Compose adapter, single service restart only | Less machinery than Kubernetes chaos tooling; never expose Docker control through the API. Add Toxiproxy only after the simple path is proven. |
| Graph | Typed adjacency list / NetworkX only if graph algorithms need it | Small explicit graph first avoids an unnecessary database. |
| Persistence | SQLite | Simple local history; keep versioned records and migrations. Move to PostgreSQL only for concurrent/shared deployments. |
| AI | Provider interface with `none` deterministic mode; optional OpenAI-compatible client later | No external API needed for basic operation. No tools/function calling; JSON-constrained output and evidence references only. |

Stack versions and additional dependencies must be pinned and security-scanned when implementation begins. Ditto's source-build requirements are not needed when using its prebuilt images; upstream currently documents JDK 25+ and Maven 3.9+ for building Ditto itself.

## Work Plan

| Priority | Work item | Depends on | Acceptance criteria | Suggested role |
| --- | --- | --- | --- | --- |
| P0 | Pin Ditto and preflight Compose, ports, auth, metrics, health, and Artillery prerequisites | None | Reproducible startup; five service health checks understood; metrics reachable only on loopback; bounded baseline workload succeeds | Integration |
| P1 | Build read-only adapter, source inventory, graph, and health/metrics normalization | P0, contracts | Graph labels facts vs inference; adapter makes no writes; fixtures test all five Ditto services | Platform/observability |
| P1 | Capture baseline and persist workload/metric windows | P0, P1 adapter | Repeatable baseline record with workload and metric coverage; missing data visible | Observability |
| P2 | Candidate generator, transparent ranking, and safety-policy evaluator | P1 graph/baseline | Test proves novel uncertain candidate ranks above a repeated low-information candidate; unsafe target is rejected | AI/platform + safety |
| P2 | Human approval and audit state | Contracts, policy | No execution path without current approval; expired/rejected approvals fail closed | Safety/API |
| P3 | One restart experiment and guaranteed cleanup/recovery check | P0, P2 approval | Disposable local run only; target restarts; canary and `/alive` recover; interruption/cleanup evidence recorded | Chaos/integration |
| P3 | Evaluation, score, findings, report | P1 telemetry, P3 run | Formula reproducible from stored inputs; each finding links raw observation IDs | Observability/analysis |
| P4 | Minimal dashboard and demo rehearsal | Stable API/report | Operator can inspect graph, approve, watch status, and export result | UI/demo |
| P4 | Optional LLM explanations and remediation | Evidence-backed findings | LLM off works; output schema/evidence validation rejects unsupported claims | AI |

No people are assigned until the team supplies names. Module ownership is role-based in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Definition of Done for This Stage

The verified target/integration facts, architecture, contracts, safety policy, initial metrics formula, team workflow, local setup approach, tests, and first milestone exist. No full application, Compose root stack, public remote, or publication is claimed.