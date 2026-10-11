# Stretch-goal run records (Mac, 2026-10-11)

Real run records and their raw probe samples from the live stretch-goal runs on a MacBook Air (Docker Desktop,
pinned Ditto 3.9.8). `data/experiments/` is git-ignored, so the records are copied here so reviewers can re-check
the verdicts without a running stack. Every run had a fresh baseline, a dry run and a typed human approval.
Like the other Mac records, these are **not** the team's official Windows-host numbers; do not mix them.

| Folder | Run | Experiment | Outcome |
|---|---|---|---|
| `rerun-after-fix/` | `run-68b41cfba584` | pause `things-search` 30 s, fix **off** | completed; 8 failing searches, all client timeouts after 3.0 s; 0 write errors; recovery check 23.0 s |
| `rerun-after-fix/` | `run-07544c127e3d` | pause `things-search` 30 s, `search-fail-fast` **on** | completed; 15 failing searches, all HTTP 504 after 1.5 s; 0 write errors; recovery check 22.8 s |
| `combined/` | `run-e075dab30e40` | solo `traffic_burst` on nginx, 45 s | completed; 0 errors anywhere (1,144 burst requests); recovery check 3.4 s |
| `combined/` | `run-a564ac28867c` | solo pause `things-search` 30 s | completed; 8 failing searches (client timeouts); 0 write errors; recovery check 22.8 s |
| `combined/` | `run-8f06b01db01a` | **combined**: burst at +0 s, pause at +10 s | completed; both faults rolled back at their own ends; 8 failing searches; 0 write errors; verdict `no_interaction` |

`samples/<run_id>.jsonl` holds the write and search probes from 5 s before injection to 5 s after recovery
(`python -m ai.request_evidence`). Re-check from the repository root:

```bash
python -m ai.remediation verify search-fail-fast --before docs/evidence/mac-local/stretch-goals/rerun-after-fix/run-68b41cfba584.json --after docs/evidence/mac-local/stretch-goals/rerun-after-fix/run-07544c127e3d.json --before-samples docs/evidence/mac-local/stretch-goals/rerun-after-fix/samples/run-68b41cfba584.jsonl --after-samples docs/evidence/mac-local/stretch-goals/rerun-after-fix/samples/run-07544c127e3d.jsonl
python -m ai.compound docs/evidence/mac-local/stretch-goals/combined/run-8f06b01db01a.json docs/evidence/mac-local/stretch-goals/combined/run-e075dab30e40.json docs/evidence/mac-local/stretch-goals/combined/run-a564ac28867c.json
```

Expected: `VERIFIED` and `no_interaction`. The before-run's samples stop at the moment recovery was verified; a fix
deploy straight after that run briefly dropped the API (documented in `docs/rerun-after-fix.md`).
