# Findings Report Contract

P3's report generator is the sole owner of finding creation. P4 diagnosis enriches
those report findings; it does not write a second diagnosis record as a finding.
The P4 helper is `ai.findings.enrich_findings`. It accepts the existing findings,
the originating run record, and deterministic diagnosis output, and returns the
same records with comparison and evidence-window data attached.

## Evidence and Status

- `prediction_comparison` is copied from diagnosis and uses the existing per-SLI
  predicted, observed, and verdict values.
- `evidence_links` use the run's `run_id` as `result_id`. A metric link is emitted
  only for a measured SLI and an observation with explicit `phase: fault`,
  `window_start`, and `window_end` values.
- `evidence_status` preserves `experiment`, `kill_switch_test`, or `inconclusive`.
  Inconclusive findings use informational severity and a deterministic request
  for complete evidence; they are not scored as success or failure.
- Findings for dry runs and forced kill-switch tests must not enter real-run
  scoring or planner history.

The enrichment code is deterministic and works with `AI_PROVIDER=none`. It does
not call an LLM or infer missing evidence. If future LLM wording is added, validate
its output against the finding contract and reject evidence references not present
in the run before falling back to deterministic text.

## P2 Contract Proposal

The current finding schema rejects additional properties. Proposed additive
schema version **1.1** adds optional `prediction_comparison`, `evidence_links`,
and `evidence_status`; new enriched findings should populate them. Keep 1.0
findings readable during migration. The OpenAPI response should continue returning
the same finding records and expose the additional optional fields through the
versioned schema. Dashboard and export readers should consume that one contract.

Schema 1.1 and the API/dashboard fields are proposed for P2; ai/report.py creates findings and
enrich_findings adds diagnosis data to them.