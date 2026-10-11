# Metrics and Resilience Scoring

## Measurement Plan

> **Implemented today:** the three SLIs come from our own probes, not Artillery or `/alive`: write (success rate,
> p50/p95 of telemetry writes), search (success rate, latency of fleet search queries) and freshness (staleness of
> each change on Ditto's SSE stream), all as JSON Lines in `data/metrics/*.jsonl`. Recovery time runs from rollback
> until health, an authenticated canary and the SLIs are back within thresholds. The table below is the original
> measurement design; the implemented score formulas are further down.

Collect a healthy baseline before an experiment and compare equivalent workload, Ditto image/version, graph, and time windows. Initial defaults are 120 seconds baseline, 60 seconds observation window, 120 seconds recovery timeout, and 5-second scrape interval; tune after measured preflight. Preserve UTC timestamps and metric labels.

| Measurement | MVP source | Definition |
| --- | --- | --- |
| Service availability | Container health and `/alive` per service | Healthy samples / expected samples; label probe scope. `/alive` is not an API success check. |
| Request success rate/error rate | Bounded Artillery/API canary result | Successful responses / completed requests; retain status-code classes and request count. |
| Latency p50/p95 | Artillery response observations; Ditto `roundtrip_http_seconds` histogram as a separate service metric | Report client-observed and Gateway-observed latency separately; do not mix their scopes. |
| Detection time | Timestamped health/metric/threshold events | Injection start to first configured, sustained detection signal. State polling interval and threshold. |
| Recovery time | Fault stop timestamp to healthy service plus successful functional canary | Both checks must pass; record each separately and the later recovery time. |
| Propagation / affected services | Per-service health/API canaries; graph | Count non-target monitored nodes with a threshold breach during the experiment window; retain source and confidence. |
| Resource use | Ditto JVM metrics and Docker stats if available | JVM memory/threads/GC are documented; host/container CPU/memory visibility needs local runtime probes and remains an optional preflight check. |
| Prediction accuracy | Predicted component impacts vs observed affected components | Multi-label precision, recall and (when probabilities are calibrated) Brier score across runs; report sample count. |
| Safety | Policy/audit records | Rejections, aborts, violations, cleanup attempts and cleanup success; never hide these in an average score. |
| Recommendation quality | Human reviewer rubric | Rate evidence, relevance, and actionability 1-5; report reviewer count and do not combine with the resilience score. |

Ditto's official operations docs describe Prometheus metrics on each service's port `9095`, path `/`, including JVM metrics, Gateway request round-trip/status metrics, persistence metrics, and Connectivity processing/mapping metrics. Confirm actual names, units, labels, and availability on the pinned release. Per-service metrics ports are not published to the host by the default Compose sample. The configuration's `19095`-`19099` loopback endpoints are planned mappings, not upstream defaults.

## Reproducible Score

Scores are never produced for dry runs: a dry run exercises preflight and records the commands that would run, but it does not create fault evidence. Forced kill-switch tests are also excluded because they validate the safety path rather than resilience. Availability uses the worst observed error rate across both write and search paths, so a search outage cannot appear healthy merely because writes continued. Detection is recorded as `1.0` for a completed run and `0.5` when an abort reason is recorded; this project convention rewards completed operation while retaining evidence that the kill switch detected a problem. Runs with no fault observations remain `incomplete`.

### Implemented formulas (score version 1.0, `safety/scoring.py`)

The list below is the original design. The runner implements these simplified rules, and each run record stores their raw inputs in `score_inputs` (shown in the dashboard's run view):

- Availability = `1 - max(worst write error rate, worst search error rate)` over the fault windows.
- Latency = `baseline write p95 / worst write p95` during the fault, capped at 1. Search p95 is not used because its baseline differs from the write baseline.
- Recovery = `1 - recovery_time_seconds / recovery budget` (policy `recovery.timeout_seconds`, 180 s). Only when recovery was verified.
- Containment = 1 if every fault window kept unrelated components healthy, else 0.
- Detection = 1.0 for a completed run, 0.5 for a run the kill switch aborted.
- Weights 0.30 / 0.20 / 0.20 / 0.15 / 0.15 as below. A component with no recorded input is left out, never guessed, and the score is `incomplete` below 0.80 weight coverage.

The score is a project-defined demo index, not a recognized industry standard or SLA. For each experiment, compute five normalized dimensions in $[0,1]$:

- Availability $A = \min(1, observed\_success\_rate / target\_success\_rate)$.
- Latency $L = \min(1, p95\_latency\_budget / observed\_p95)$; score is missing if there are too few requests or no valid p95.
- Recovery $R = \max(0, 1 - recovery\_seconds / recovery\_budget)$. If functional recovery does not occur by the deadline, $R=0$ and mark recovery failed.
- Containment $C = 1 - affected\_nonTarget\_services / monitored\_nonTarget\_services$. Missing if the denominator is zero or monitoring coverage is inadequate.
- Detection $D = \max(0, 1 - detection\_seconds / detection\_budget)$. If there is no detection before the budget expires, $D=0$.

Starting weights: $w_A=0.30$, $w_L=0.20$, $w_R=0.20$, $w_C=0.15$, $w_D=0.15$. Each budget and target is stored with the run; these are local project choices to review after baseline, not industry constants.

$$
Score = 100 \times \frac{\sum_{i \in observed} w_i s_i}{\sum_{i \in observed} w_i}
$$

Publish the component values and coverage `sum(observed weights) / 1.0` alongside the result. Show `incomplete` rather than a numeric score if coverage is below 0.80, the baseline is invalid, or the functional recovery/measurement result is missing. If any safety violation occurred, suppress the score and mark the run unsafe; do not turn unsafe behavior into a merely low score. Missing metrics are never imputed as success. A high availability score cannot cancel failed recovery because every component remains visible.

Report raw counts, thresholds, workload rate, run windows, score version, graph version, Ditto tag, and metric query window so another teammate can recompute the result. Keep the security/dependency assessment separate: it produces evidence-based findings, not a component of resilience score.

## Baseline Comparison and Limitations

Run at least two healthy baseline captures when time allows; use their median and preserve spread. Compare the same synthetic dataset, load scenario, rate, duration, Docker resource allocation, and Ditto tag before and after fault. Do not compare different machines as if the workload were identical. With only one hackathon run, label outcomes exploratory. Prometheus scrape intervals and Artillery sampling bound the precision of time-to-detect/recover. Missing service metrics and uncertain graph edges reduce score coverage instead of becoming invented values.