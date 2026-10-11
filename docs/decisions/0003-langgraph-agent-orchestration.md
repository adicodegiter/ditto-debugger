# ADR 0003: Optional LangGraph Orchestration of the Advisory Agents

- **Status:** accepted
- **Date:** 2026-10-11

## Context

The three agents were coordinated by plain method calls in `backend/app/orchestrator.py`. The planning gates
(graph available, baseline fresh, telemetry coverage at least 80%, graph version match) lived inside the planner,
and the human-approval pause was implicit. The team wanted the multi-agent flow to be explicit and inspectable
without weakening the rule that agents never execute.

## Decision

Add `backend/app/graph.py`, a LangGraph `StateGraph` whose nodes **wrap** the existing orchestrator methods:
`discover` → `measure` → `plan` → `approval`. The gates become conditional edges (no graph ends the run; the first
`measure` pass reuses the supplied or latest stored baseline, and if the gate rejects it the graph routes back to
`measure` once to capture a fresh one, or ends with the reason; a replaced baseline is named in `baseline_note`). The graph is compiled with
`interrupt_before=["approval"]` and ends at approval. A second graph runs post-run analysis:
`diagnose` → `compare` → `report`.

- LangGraph is **optional**: pinned `langgraph==1.2.12` (released 2026-09-21) in `requirements-graph.txt`. Without
  it, the same node functions run in sequence with the same routing.
- No node imports or calls the runner, Docker adapter, kill switch, watchdog or operator CLI; tests check the
  module's imports, its code and each node callable.
- Nodes are deterministic. The optional Groq rewording is used only in the analysis `report` node through
  `ai/explain.py`, with its schema check and no-key fallback.
- The API exposes `GET /api/v1/agent-graph` (read-only) and `POST /api/v1/agent-graph/run` / `resume` (advisory,
  same writes as baseline + plan); the dashboard shows it under Experiments → Agent graph.

## Consequences

The decision flow is visible (node trace, gate reason, pause point) in the CLI, API and dashboard, and the gates are
tested as routing. Execution is unchanged: only `python -m safety.cli execute` with a typed approval. The extra
dependency is opt-in, so the base install and the test suite work without it. The in-memory checkpointer keeps one
"last run" per API process, which is fine for a single-operator local demo but would need per-user threads for
shared use.
