# Combined failures (stretch goal)

Brief: "Run multiple failures together (e.g. traffic spike while a service is down)."

One approved, immutable plan holds **two staged faults**: for example `traffic_burst` on nginx at
t = 0 s and `pause_container` on `things-search` at t = 10 s. One kill switch watches the whole
run, and every fault is rolled back. Nothing about the safety model is relaxed; the compound path
adds rules on top of the single-fault ones.

## Why this pairing

Metastable-failure research (a trigger such as a load spike or lost capacity pushes a system into
a degraded state that sustains itself, often through client retries) says the dangerous cases are
combinations that a single-fault test never reaches. Entry-point load plus a degraded downstream
service is the textbook one. The compound run therefore starts the load first, so the downstream
fault lands on an already loaded system, and then asks one question: **is the pair worse than
either fault alone?**

## Workflow

```bash
python -m safety.cli baseline                                  # fresh baseline (needs the workload running)
python -m safety.cli plan BASELINE_ID                          # single-fault candidates, as before
python -m safety.cli suggest-compound BASELINE_ID              # policy-allowed pairs, amplifying first
python -m safety.cli compose BURST_ID SEARCH_ID --stagger 10   # builds ONE compound plan; executes nothing
python -m safety.cli approve COMPOUND_ID --by NAME --reason TEXT   # human types APPROVE
python -m safety.cli execute COMPOUND_ID --dry-run             # prints the exact commands, changes nothing
CHAOS_EXECUTION_ENABLED=true python -m safety.cli execute COMPOUND_ID
```

The interaction verdict needs solo runs of both faults in the run store (run each single fault
first, as in `docs/m5-results.md`). Without them the verdict is `inconclusive`, never a guess.

## Rules enforced by the control plane (`safety/policy.py`)

| Rule | Where it comes from |
|---|---|
| Combined failures must be enabled and the pair listed in `compound.allowed_pairs` (unordered `action:target`) | `configs/experiment-policy.yaml` |
| Exactly two faults, never two on one target (their rollbacks would conflict) | policy, hard ceiling in code |
| MongoDB can never be part of a combination (checked at policy load and per candidate) | policy |
| Every fault is individually valid: enabled action, allowlisted target | same checks as single faults |
| Each fault ends inside `max_duration_seconds` (60 s); start offsets ≤ `max_stagger_seconds` (20 s) | policy |
| Combined risk `1 − ∏(1 − rᵢ)` must equal the stated value and be ≤ `compound.max_risk_score` (0.45) | policy |
| The approval hash covers every fault, so changing one invalidates it; approvals are single use | runner |
| `--dry-run` and `--forced-test` are never scored; the API `execute` is still 503 | runner, API |

## What the runner does (`safety/runner.py::_run_compound`)

1. Preflight **every** target; steady-state check; refuse if unhealthy.
2. Capture rollback state and **arm one watchdog per fault** before touching anything.
3. Inject each fault at its own offset (marked injected *before* the command runs).
4. Poll the one kill switch every 2 s. "Unrelated component healthy" excludes **all** fault targets.
   Each observation records which faults were active (`active_faults`).
5. A fault that reaches its own end is rolled back then; any abort rolls back everything still active.
6. Rollback is in **reverse injection order**. Every rollback is attempted even if another fails;
   a fault whose rollback failed keeps its watchdog armed and the run is `failed`.
7. Recovery needs `/health`, the canary **and** SLIs back within baseline, for every target.

## How the AI decides (`ai/compound.py`, deterministic, no LLM needed)

- **Suggest**: among planned candidates, only policy-allowed pairs; pairs that are predicted to
  hurt a *shared* SLI are ranked first (`amplified` expectation), then lowest combined risk.
- **Predict**: worst-of per SLI across the faults, plus a written rationale.
- **Judge** (`interaction_effect`): per SLI metric, compare the compound run with the worst solo run.
  - `amplified`: > 25 % **and** an absolute margin (10 pp error rate, 100 ms p95, 2 s freshness) worse than every solo run. The margin stops noise on a near-zero metric counting.
  - `dampened`: one fault masked the other. `no_interaction`: inside the band.
  - `inconclusive`: dry run, forced test, missing solo run or no comparable SLI.
- **Finding**: an `amplified` verdict becomes a high-severity finding linking the compound run and
  the solo runs it was compared against.

`python -m ai.compound COMPOUND_RUN.json SOLO_RUN.json [...]` prints the verdict for saved records.

## Live result (Mac stack, 2026-10-11)

Three real runs, each with a fresh baseline, a dry run and a typed approval:

| Run | What | Outcome |
|---|---|---|
| `run-e075dab30e40` | solo `traffic_burst` on nginx, 45 s (about 25 extra req/s, 1,144 requests, 0 failed) | completed; 0 % errors; write p95 34 ms during the fault (raw samples); recovery check 3.4 s |
| `run-a564ac28867c` | solo `pause_container` on things-search, 30 s | completed; 8 searches failed (client timeouts at 3 s), 0 write errors; raw write p95 48 ms; recovery check 22.8 s after unpause |
| `run-8f06b01db01a` | **compound**: burst at +0 s, pause at +10 s for 30 s | completed; faults staged and rolled back at their own ends (pause at +40 s, burst at +45 s); both watchdogs armed and disarmed; 8 searches failed (client timeouts), 0 write errors; raw write p95 36 ms; recovery check 8.0 s after unpause |

**Interaction verdict: `no_interaction`** (5 of 5 SLI metrics). At about 31 req/s in total, Ditto
absorbs the burst, so the pair is no worse than the pause alone. A load the stack actually feels
(the policy hard-caps the burst at 50 req/s per loop) is needed to look for amplification.

Lessons from the live run:

- **"Recovery time" is mostly health-reporting lag, not an outage.** In both pause runs the first
  search after the unpause already succeeded. The 8–23 s is how long the recovery check
  (`/health`, canary and SLIs) took to pass. Docker also reported things-search `unhealthy` for a
  while after each pause. Compound runs now record `recovery_after_own_rollback_seconds` per fault,
  because the run-level figure counts from the last rollback (3.4 s here, against 8.0 s from the unpause).
- **The kill switch's 30 s windows leak pre-fault data.** The compound's worst window said write
  p95 84 ms, but raw writes during the fault had p95 36 ms. Writes in the 60 s before injection had
  p95 68 ms. The interaction verdict still reads these windows, and its 100 ms margin kept this
  from becoming a false "amplified". Moving it to raw samples (as `ai/remediation.py` does on the
  fix branch) is the next step.
- **A bug the live run found:** solo runs were not matched, because real records carry action and
  target only in the diagnosis and experiment id. Fixed, with a test using the real record shape.

## Status and limits

- Policy, composition, staged runner, rollback ordering, abort paths, recovery and the interaction
  verdict are covered by `tests/test_compound.py` (fake Docker, fake clock).
- Run live once (above): mechanics verified. One run per side, so the verdict is indicative.
- Two faults only, one pairing family (burst on nginx with a `things-search` fault). Adding a
  pair is a policy edit, but each new pair deserves its own review.
