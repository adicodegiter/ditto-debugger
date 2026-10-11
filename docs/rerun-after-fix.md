# Re-run after applying a fix (stretch goal)

Brief: "Re-run experiments after applying a fix. Show improvement in system behaviour after
implementing the fix."

## The loop

1. **Weakness** (from a real run): `docs/findings-report.md` names *"search does not fail fast
   while things-search is unavailable"*. The 5.7 re-run fixed the client side (10 s → 3 s), but
   every failure was still a client timeout. Ditto's gateway holds the search, and nginx allows it
   70 s, so any client without its own timeout hangs.
2. **Fix** (registered in `configs/remediations.yaml`, applied by `deploy/remediate.py`):
   `search-fail-fast` adds one nginx location for `/api/2/search`. A search that takes longer than
   **1.5 s** gets an explicit **HTTP 504** with `Retry-After: 2`, so clients back off instead of
   retrying into a stuck service (the retry-storm pattern behind most metastable failures). Writes,
   reads and WebSockets are untouched.
3. **Re-run** the same experiment (`pause_container` on `things-search`, 30 s) with the fix on.
   Each run records which fixes were live, read from the running nginx (`deployment.remediations_active`).
4. **Verdict** (`python -m ai.remediation verify`): the fix is `verified` only if
   - **conditions** hold: both runs are real (no dry run or forced test), the same fault, target
     and duration, and the fix was off before and on after;
   - **success criteria** pass: the longest failing search is ≤ 2.0 s and shorter than before,
     and every failure is an HTTP 504, not a client timeout;
   - **no-regression guards** pass: write errors rise by at most 2 points, write p95 (as a multiple
     of each run's own baseline) grows by at most 1.5×, and the after run recovers.

   Missing evidence gives `inconclusive`, never a pass. A failed check gives `not_verified`.

## Why 1.5 s

- Healthy searches through nginx on the Mac stack: p95 49 ms, slowest 213 ms (246 samples).
  1.5 s is about 7× the slowest one.
- It is below the probe's own 3 s timeout, so the server answers first and the change is visible.
- The first search after nginx is recreated took 1.04 s, close to the cap. Run a few searches (or
  wait for the workload) after `apply` before taking a baseline.

## Commands

Run from the repository root with the project's Python, in a second terminal, while the
workload runs in the first:

```bash
python simulator/fleet_workload.py --duration-seconds 2400 --metrics-dir data/metrics
```

**Before** (fix off):

```bash
python deploy/remediate.py status
python -m safety.cli baseline
python -m safety.cli plan BASELINE_ID --max-candidates 10
python -m safety.cli approve PAUSE_ID --by NAME --reason "before fix"
python -m safety.cli execute PAUSE_ID --dry-run
CHAOS_EXECUTION_ENABLED=true python -m safety.cli execute PAUSE_ID
```

**Apply the fix**, wait about 5 minutes so the next baseline window is clean, then **after**:

```bash
python deploy/remediate.py apply search-fail-fast
python -m safety.cli baseline
python -m safety.cli plan BASELINE_ID --max-candidates 10
python -m safety.cli approve PAUSE_ID --by NAME --reason "after search-fail-fast"
CHAOS_EXECUTION_ENABLED=true python -m safety.cli execute PAUSE_ID
```

**Report:**

```bash
python -m ai.remediation verify search-fail-fast --before data/experiments/BEFORE_RUN.json --after data/experiments/AFTER_RUN.json --markdown data/remediation/search-fail-fast.md
```

Probe samples are cut from `data/metrics/` automatically. To keep them with the evidence:
`python -m ai.request_evidence --run data/experiments/RUN.json`.

**Undo:** `python deploy/remediate.py revert search-fail-fast`. Only nginx is recreated.

**Apply and revert briefly drop the API.** Recreating nginx closes every open connection. With Docker's default 10 s stop timeout the workload's open SSE stream held nginx for the full 10 s: in the first live before-run, applying the fix straight after the run cost about 10 s with no API (14 `ConnectionError`s, 23:50:13–23:50:23 UTC). `remediate.py` now stops nginx with a 2 s timeout, refuses to run while `safety.cli execute` is running, and logs every apply/revert to `data/remediation-events.jsonl`. The verifier only counts samples up to the moment recovery was verified, so a deploy after a run never counts for or against the fix. Wait about 5 minutes after `apply` before the next baseline.

The client-side fix from 5.7 can be re-checked from the committed records at any time:

```bash
python -m ai.remediation verify search-client-timeout --evidence
```

That prints **VERIFIED**: the longest failing search fell from 10.0 s to 3.0 s, write errors did
not rise and the after run recovered.

## Safety

- `deploy/remediate.py` can apply only fixes listed in the registry, only overlays inside this
  repo, and recreates only the one service named (`--no-deps`). Its health check only contacts
  a loopback URL. It is a human deploy step like `deploy/reset.sh`; agents and the API never call it.
- The overlay is the base `nginx.conf` plus one marked block. `tests/test_remediation.py` fails if
  anything else differs, if search loses authentication, or if the timeout is not below the probe's.
- Faults still run only through the operator CLI with a typed approval.

## Result (live, Mac stack, 2026-10-11)

`python -m ai.remediation verify search-fail-fast --before run-68b41cfba584 --after run-07544c127e3d`
gives **VERIFIED**. Both runs were `pause_container` on `things-search` for 30 s, with a fresh
baseline and a typed approval each.

| Measure | Before (fix off) | After (fix on) |
|---|---|---|
| Fix live when the run started (read from nginx) | none | `search-fail-fast` |
| How a failing search ends | client `ReadTimeout` (8 of 8) | **HTTP 504 from nginx** (15 of 15) |
| Longest failing search | 3.0 s (the client's own limit) | **1.5 s** |
| Write errors during the fault | 0 % | 0 % |
| Write p95 during the fault, raw samples (n = 58 each) | 45.3 ms (0.81 × baseline) | 30.3 ms (0.80 × baseline) |
| Recovery after unpause | 23.0 s | 22.8 s |
| Search outage (first to last failure) | 27.6 s | 29.7 s |

What it shows:

- Failures are now **explicit and twice as fast**. Any client, including one with no timeout, gets
  a 504 and `Retry-After` within 1.5 s instead of hanging for up to 70 s.
- The fix does **not** shorten the outage or the recovery. `things-search` is down either way, and
  recovery stays about 23 s. The next fix to try is the slow recovery after unpause.
- **One measurement lesson.** The first verdict was NOT VERIFIED on the write-latency guard, because
  it read the kill switch's 30 s windows. Their first polls reach about 28 s back before injection,
  so one slow pre-fault write (61 ms) set the "fault" value. Raw writes inside the fault were faster
  with the fix on, and the guard now uses raw samples, with like-for-like sources on both sides.
  The same window leak affects the score's latency component and the diagnosis. That is a known
  limitation, not changed here.

## Status

- Mechanics verified live on the Mac stack (2026-10-11): `apply` put the marker in the running
  nginx, and healthy searches returned 200 in 11 to 107 ms. `revert` removed the marker, and the
  stack stayed healthy. With the 2 s stop timeout, `revert` cost a 1.0 s API gap (5 failed probes at 0.2 s intervals, 2026-10-11). That was measured with the workload stopped, so no SSE stream was open. With the workload running, expect at most about 2 s.
- Live before-run `run-68b41cfba584` (fix off, pause 30 s): completed, recovery verified in 23.0 s;
  8 searches failed, every one a client `ReadTimeout` after 3.0 s.
- The client-side fix verifies on committed evidence.
- Server-side before/after pair: run and **verified** (above). One run per side, so treat the exact numbers as indicative.
