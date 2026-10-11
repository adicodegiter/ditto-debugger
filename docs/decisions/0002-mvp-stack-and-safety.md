# ADR 0002: Small Local-First MVP Stack

- **Status:** accepted for blueprint; implementation validation pending
- **Date:** 2026-10-10

## Decision

Use FastAPI/Python for orchestration, React/TypeScript for a later thin dashboard, Prometheus for metric history, SQLite for experiment/control records, and the upstream Ditto Artillery tool for bounded load. Begin with a typed adjacency list and deterministic candidate scoring; add NetworkX only if a required graph operation justifies it. Keep LLM use optional and evidence-constrained. The first chaos action is a short, allow-listed local service restart with explicit human approval and recovery verification.

## Consequences

The stack is testable on a laptop and needs no paid model or cloud service. The current repository includes only contracts/config/tests, not these application dependencies. Docker control remains a powerful host capability, so the runner must be a separate local process and the model/API must have no Docker socket or arbitrary execution access. Network faults and MongoDB/host resource exhaustion are deferred until specific rollback and blast-radius controls are proven.

## Update 2026-10-11

What was built differs from this plan in three places: the dashboard is plain HTML/CSS/JS (no React), the workload
and probes are our own Python (`simulator/`, `monitor/`) writing JSON Lines instead of Artillery, and Prometheus is
optional (baselines and the kill switch read `data/metrics/*.jsonl`). The fault catalog grew to five fixed actions
after their rollbacks were proven (restart, stop, pause, CPU throttle, traffic burst); MongoDB stays denied except
for a CPU throttle. Agent orchestration can optionally use LangGraph ([ADR 0003](0003-langgraph-agent-orchestration.md)).
