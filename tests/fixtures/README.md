# Run fixtures

These are reconstructed acceptance fixtures, not copies of raw safety-runner records. Each JSON fixture is labelled with `_fixture: true` and `_source: "reconstructed from docs/m4-results.md, not a raw record"`.

- `m4-normal-docs-reconstruction.json` preserves the documented run ID and reported normal-run facts. `observations` is empty because the original per-poll observations and raw metric files are not available. The reported p95 is kept as an upper bound, not an invented exact measurement.
- `m4-forced-test-docs-reconstruction.json` preserves the documented forced-test run ID, runner markers, recovery fields, and reported timing facts. Missing timestamps and per-poll observations remain absent.
- `m4-dry-run-docs-note.json` records only that the report says both rehearsals passed. It has no run ID or measurements because the report provides no individual dry-run record.

Tests must read these fixtures rather than `data/experiments/`. The latter is ignored by Git and is absent in clean checkouts and CI. Missing fixture evidence must remain inconclusive; these reconstructions must not be treated as live experiment results or scored.
