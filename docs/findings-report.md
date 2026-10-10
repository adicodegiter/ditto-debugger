# Findings Report

## Context

This deterministic report covers only the available local run artifacts. Live experiment records and saved diagnoses are distinct from reconstructed fixtures; no fault is executed while generating this report.

## Data Provenance

- **Live:** primary JSON records in `data/experiments/`, matching saved diagnoses in `data/diagnoses/`, and the ranked findings JSON in `data/findings-ranked.json`.
- **Fixture:** the Day-1 retest measurements and forced kill-switch record are reconstructed fixtures, not raw live records. The Day-1 raw record is absent; the forced-test record is reconstructed from docs/m4-results.md. [run: run-c8b30b07c9f3] [run: run-d3f744a96f7a]
- Verification sidecars are excluded from the live run count.

## Summary

- Live runs reviewed: [run: run-370897363c60], [run: run-4083aa7e13a1], [run: run-a09cb1bcbb7c]

## Fault-Type Coverage

| Fault type | Status | Live run |
|---|---|---|
| container interruption | recorded | [run: run-a09cb1bcbb7c] |
| pause | recorded | [run: run-370897363c60] |
| CPU throttle | NOT RUN | none |
| burst | recorded | [run: run-4083aa7e13a1] |

## Predicted vs Observed

### pause_container on things-search [run: run-370897363c60]
- write: predicted hold; observed hold; match [run: run-370897363c60]
- search: predicted fail; observed fail; match [run: run-370897363c60]
- freshness: predicted hold; observed hold; match [run: run-370897363c60]
- Recovery: recovered, 23.641 s. [run: run-370897363c60]
- Baseline ID: Not recorded in the run JSON; diagnosis has no baseline values. [run: run-370897363c60]

### traffic_burst on nginx [run: run-4083aa7e13a1]
- write: predicted degrade; observed hold; better_than_predicted [run: run-4083aa7e13a1]
- search: predicted degrade; observed hold; better_than_predicted [run: run-4083aa7e13a1]
- freshness: predicted degrade; observed hold; better_than_predicted [run: run-4083aa7e13a1]
- Recovery: recovered, 4.297 s. [run: run-4083aa7e13a1]
- Baseline ID: Not recorded in the run JSON; diagnosis has no baseline values. [run: run-4083aa7e13a1]

### restart_container on things-search [run: run-a09cb1bcbb7c]
- write: predicted hold; observed hold; match [run: run-a09cb1bcbb7c]
- search: predicted fail; observed hold; better_than_predicted [run: run-a09cb1bcbb7c]
- freshness: predicted hold; observed hold; match [run: run-a09cb1bcbb7c]
- Recovery: recovered, 22.359 s. [run: run-a09cb1bcbb7c]
- Baseline ID: Not recorded in the run JSON; diagnosis has no baseline values. [run: run-a09cb1bcbb7c]

## Ranked Findings

- **MEDIUM** The approved experiment caused measurable client-side availability degradation. [run: run-370897363c60]
  - Recommendation: Investigate the affected dependency and add an availability budget or timeout protection before increasing fault duration. [run: run-370897363c60]
  - Evidence: search_error_rate=0.8888888888888888; source `data/experiments/run-370897363c60.json`; window 2026-10-10T17:11:01.187916+00:00 to 2026-10-10T17:11:31.187916+00:00 [run: run-370897363c60]
- **MEDIUM** The approved experiment caused measurable client-side availability degradation. [run: run-a09cb1bcbb7c]
  - Recommendation: Investigate the affected dependency and add an availability budget or timeout protection before increasing fault duration. [run: run-a09cb1bcbb7c]
  - Evidence: search_error_rate=0.047619047619047616; source `data/experiments/run-a09cb1bcbb7c.json`; window 2026-10-10T18:20:37.499052+00:00 to 2026-10-10T18:21:07.499052+00:00 [run: run-a09cb1bcbb7c]

## Informational Results

- **INFO** The system absorbed more than predicted on: freshness (predicted degrade, observed hold), search (predicted degrade, observed hold), write (predicted degrade, observed hold). [run: run-4083aa7e13a1]
  - Recommendation: Update the planner's effect model, or check that the measurement window captured the full outage. [run: run-4083aa7e13a1]
  - Evidence: write_error_rate=0.0, write_p95_ms=32.495; source `data/experiments/run-4083aa7e13a1.json`; window 2026-10-10T17:27:05.694883+00:00 to 2026-10-10T17:27:35.694883+00:00 [run: run-4083aa7e13a1]
- **INFO** The system absorbed more than predicted on: search (predicted fail, observed degrade). [run: run-a09cb1bcbb7c]
  - Recommendation: Update the planner's effect model, or check that the measurement window captured the full outage. [run: run-a09cb1bcbb7c]
  - Evidence: search_error_rate=0.047619047619047616; source `data/experiments/run-a09cb1bcbb7c.json`; window 2026-10-10T18:20:37.499052+00:00 to 2026-10-10T18:21:07.499052+00:00 [run: run-a09cb1bcbb7c]

## Resilience Scores

Scores below are copied from the run records; they are not recalculated.

| Run | Recorded score | Availability | Containment | Detection | Latency | Recovery |
|---|---:|---:|---:|---:|---:|---:|
| [run: run-370897363c60] | 70.39 | 0.11111111111111116 | 1.0 | 1.0 | 0.9842646250158623 | 0.8686611111111111 |
| [run: run-4083aa7e13a1] | 91.68 | 1.0 | 1.0 | 1.0 | 0.6078165871672566 | 0.9761277777777778 |
| [run: run-a09cb1bcbb7c] | 89.0 | 0.9523809523809523 | 1.0 | 1.0 | 0.6457913339442335 | 0.8757833333333334 |

## Forced Kill-Switch Test

- **Fixture record, not live data:** restart_container on things-search; score: **Not scored by design**. Excluded from live findings and resilience scores. [run: run-d3f744a96f7a]

## Remediation Retest

- Before-run measurements are a labelled fixture reconstruction; after-run measurements use the live run record where available. Supplemental request-level measurements are fixture/documented values. [run: run-c8b30b07c9f3] [run: run-a09cb1bcbb7c]
- The live after record omits its baseline ID, although the supplemental summary names one; the discrepancy is retained below. [run: run-a09cb1bcbb7c]

| Measure | Before | After | Delta |
|---|---|---|---|
| Total search outage | 21.0 s [run: run-c8b30b07c9f3] | not recorded [run: run-a09cb1bcbb7c] | not demonstrated |
| Last failed search after rollback | 2.0 s [run: run-c8b30b07c9f3] | 7.0 s [run: run-a09cb1bcbb7c] | 5.0 s |
| Wait for one failed search | 10.0 s [run: run-c8b30b07c9f3] | 3.0 s [run: run-a09cb1bcbb7c] | -7.0 s |
| Search latency p95 | not recorded [run: run-c8b30b07c9f3] | not recorded [run: run-a09cb1bcbb7c] | not demonstrated |
| Write error rate | 0.0 ratio [run: run-c8b30b07c9f3] | 0.0 ratio [run: run-a09cb1bcbb7c] | 0.0 ratio |
| Write p95 | 19.609 ms [run: run-c8b30b07c9f3] | 38.195 ms [run: run-a09cb1bcbb7c] | 18.586 ms |
| Recovery after rollback | 4.188 s [run: run-c8b30b07c9f3] | 22.359 s [run: run-a09cb1bcbb7c] | 18.171 s |
| Search request timeouts | 2/2 over 24.9 s [run: run-c8b30b07c9f3] | 5/5 over 44.1 s [run: run-a09cb1bcbb7c] | +3 requests; windows differ [run: run-c8b30b07c9f3] [run: run-a09cb1bcbb7c] |

- Conditions: same fault = yes; same target = yes; same duration = yes. [run: run-c8b30b07c9f3] [run: run-a09cb1bcbb7c]
- Client timeout: 10.0 s to 3.0 s; delta -7.0 s. [run: run-c8b30b07c9f3] [run: run-a09cb1bcbb7c]
- Baseline write p95: 15.734 ms before, 24.666 ms after. Delta 8.932 ms. [run: run-c8b30b07c9f3] [run: run-a09cb1bcbb7c]
- Baseline IDs in raw run records: before `not recorded`, after `not recorded`; supplemental after summary: `baseline-c290fc21633a`. [run: run-c8b30b07c9f3] [run: run-a09cb1bcbb7c]
- Conclusion: per-failed-request wait fell from 10.0 s to 3.0 s, but overall resilience improvement is **not demonstrated**. Search outage duration and search latency p95 are not recorded for both runs. [run: run-c8b30b07c9f3] [run: run-a09cb1bcbb7c]

## Evidence Gaps

- CPU throttle: no live run is present in `data/experiments/`.
- Baseline IDs: absent from the raw run records for the live experiments; diagnosis warnings note missing baseline values for each SLI.
- Day-1 retest raw record and raw request-level JSONL are unavailable in the working tree; the before values are fixture/documented reconstructions. [run: run-c8b30b07c9f3]
- Search outage total and search latency p95 after the retest are not recorded. [run: run-a09cb1bcbb7c]
- Scores: all available live experiment records include a scalar resilience score and score components; the forced test is not scored by design.

## Source Files

- Live run: `data/experiments/run-370897363c60.json`; saved diagnosis: `data/diagnoses/diagnosis-run-370897363c60.json`. [run: run-370897363c60]
- Live run: `data/experiments/run-4083aa7e13a1.json`; saved diagnosis: `data/diagnoses/diagnosis-run-4083aa7e13a1.json`. [run: run-4083aa7e13a1]
- Live run: `data/experiments/run-a09cb1bcbb7c.json`; saved diagnosis: `data/diagnoses/diagnosis-run-a09cb1bcbb7c.json`. [run: run-a09cb1bcbb7c]
- Ranked live findings: `data/findings-ranked.json`. [run: run-370897363c60] [run: run-4083aa7e13a1] [run: run-a09cb1bcbb7c]
- Forced-test fixture: `tests/fixtures/m4-forced-test-docs-reconstruction.json`. [run: run-d3f744a96f7a]
- Retest fixtures: `tests/fixtures/restart-retest-before.json` and `tests/fixtures/restart-retest-after.json`. [run: run-c8b30b07c9f3] [run: run-a09cb1bcbb7c]
