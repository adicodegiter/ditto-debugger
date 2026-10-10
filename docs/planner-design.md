# Planner and diagnosis design note (P4)

**Owner:** P4. **Files:** `backend/app/agents/planning.py`, `ai/diagnosis.py`, `ai/plan_cli.py`, `configs/planner.yaml`.
**Principle:** the planner and diagnosis are deterministic code. They never execute anything, and an optional LLM (Day 2) may only reword text. With no API key the system behaves identically.

## Inputs
| Input | Source | Used for |
|---|---|---|
| Versioned graph | discovery + mapping agents | blast radius (`criticality`), node roles |
| Baseline and telemetry | telemetry agent | planning gate (fresh, coverage >= 0.8) and the diagnosis baseline |
| Policy | `configs/experiment-policy.yaml` (P5) | allow-list, deny-list, risk cap, duration. The planner can only narrow it |
| Planner config | `configs/planner.yaml` (P4) | measured SLIs, predicted effect per target, first-run limit |
| History | `LocalRepository` | novelty and uncertainty; only conclusive runs are recorded |

## Candidates
One candidate per allow-listed target, action `restart_container` only. A target is rejected when it is not a discovered Ditto service, exceeds the risk cap, or has no predicted effect observable with the measured SLIs. Every rejection is returned with a reason.

**First-run rule:** while no experiment has completed, only candidates with impact <= `first_run_max_impact` (0.35) are eligible; the rest are listed as deferred. If none qualify, the lowest-impact candidate is used and the reason says so.

## Ranking (unchanged from `weighted_heuristic_v1`)
`score = 0.40*impact + 0.35*novelty + 0.25*uncertainty`, each term 0..1. Impact is 1.0 for the Gateway, else configured dependents / maximum dependents. Novelty is 1/(1+trials). Uncertainty is 1/sqrt(1+trials). Ties go to lower risk, then target name. Changing weights needs a decision record.

## Predictions
`predicted_slis` maps each measured SLI to `hold`, `degrade` or `fail`, from `predicted_effects` in `configs/planner.yaml`. Example: restarting `things-search` predicts search `fail`, write and freshness `hold`. The `reason` field is generated from the same terms.

## Diagnosis (one run)
Per SLI during the run: **fail** if error rate >= 20%, **degrade** if error rate rises more than 2 points above baseline or p95 exceeds max(2x baseline, baseline + 100 ms), else **hold**. Verdicts: `match`, `worse_than_predicted`, `better_than_predicted`, `not_measured`. A missing SLI is never assumed healthy.
Flags: `did_not_recover`, `slow_recovery`, `worse_than_predicted`, `kill_switch_fired`, `safety_violation`, `cleanup_not_complete`.
`evidence_use` is `experiment`, `kill_switch_test` (forced abort; proves the kill switch only) or `inconclusive` (telemetry gap or failed run). Only `experiment` runs update planner history.

## Out of scope on Day 1
Severity ranking, fix recommendations, findings report, resilience score, LLM wording (Day 2).
