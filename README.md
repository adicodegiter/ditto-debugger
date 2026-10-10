# Ditto AI Chaos Engineering

AI-driven chaos engineering for a connected-vehicle digital-twin backend, built on Eclipse Ditto (a Bosch-originated platform). Three agents map a locally running Ditto deployment from its manifests (Agent 1), measure its healthy behaviour under a simulated vehicle fleet (Agent 2), and rank safe failure experiments with per-SLI predictions (Agent 3). A deterministic control plane runs only a human-approved, immutable plan, with a kill switch, max duration, rollback and recovery check, and the results become evidence-linked findings with a resilience score. Agents never execute anything; the API's execute endpoint always returns 503, and only the operator CLI changes a container.

## Why Ditto

Eclipse Ditto is an IoT digital-twin framework with five cooperating microservices and a locally runnable Docker Compose deployment. Its industrial IoT domain is relevant to Bosch, while its service boundaries and built-in metrics make it a useful resilience target. We keep Ditto upstream code in an ignored external checkout; this repository does not fork or patch it.

## Architecture

```mermaid
flowchart LR
    D[Ditto Compose deployment] --> A[Ditto adapter]
    D --> T[Metrics and health]
    A --> G[Architecture graph]
    T --> P[Experiment planner]
    G --> P
    H[Experiment history] --> P
    P --> S[Deterministic safety gate]
    S -->|proposed| U[Human approval]
    U --> X[Restricted local runner]
    X --> D
    X --> O[Observation and evaluation]
    T --> O
    O --> R[Findings and resilience score]
    R --> UI[Dashboard and report]
    L[Optional LLM] -. evidence-bound analysis .-> R
```

See [the architecture](docs/architecture.md) and [the full blueprint](docs/project-blueprint.md).
The upstream Ditto module paths and the custom workbench code map are in [Ditto codebase map](docs/ditto-codebase-map.md); the role-based 5-person breakdown and shared setup checklist are in [team implementation plan](docs/team-implementation-plan.md).

## Current Status

- Verified upstream target: [eclipse-ditto/ditto](https://github.com/eclipse-ditto/ditto), release `3.9.8` as checked on 2026-10-10.
- Local Ditto startup, benchmark dependencies, health/metrics endpoints, license, and architectural boundaries are documented in [Ditto integration](docs/ditto-integration.md).
- JSON Schemas and examples define shared data contracts in [schemas](schemas/); Compose and Kubernetes discovery, Prometheus normalization, local SQLite baseline history, deterministic ranking, FastAPI routes, and the local dashboard are implemented.
- **M0 (Ditto runtime) verified 2026-10-10** on Docker Desktop (Windows 11, 7.6 GiB for Docker). The stack runs with the image pins from `deploy/image-pins.env`, all 5 Ditto services report healthy, and the API answers on `127.0.0.1:8080`. `simulator/smoke_test.py` passes 5/5, which confirms on the live stack:
  - the Ditto v2 API paths (`/api/2/policies`, `/api/2/things`, `/api/2/search/things`)
  - the policy body with the `nginx:ditto` subject
  - the `mongo:7.0.14` tag
- **Chaos runner.** It runs only as a separate operator process (`python -m safety.cli execute <experiment_id>`). The API never runs Docker, and its execute endpoint always returns `503`. The runner:
  - requires `CHAOS_EXECUTION_ENABLED=true`
  - polls the kill switch every 2 s during a fault, using real SLI samples from `data/metrics/*.jsonl`
  - arms an independent watchdog process before injecting a fault
  - verifies recovery after every run, including aborted runs

  `--dry-run` reads real state and SLIs but changes no container. See [docs/safety-model.md](docs/safety-model.md).
- **M1 dependency map:** [docs/dependency-map.md](docs/dependency-map.md) (Mermaid) and `ai/dependency_map.json`, generated from the pinned compose file by `python -m ai.dependency_map`. Every edge is labelled configured, documented or manual, and every Ditto node lists the SLIs it can affect.
- **M4 done on 2026-10-10:** a real `things-search` restart completed, with recovery verified 4.2 s after rollback. A forced kill-switch test aborted 2.5 s into the fault and rolled back 0.5 s later. See [docs/m4-results.md](docs/m4-results.md); to repeat it, follow [docs/m4-runbook.md](docs/m4-runbook.md).
- **Day 2 (M5) done on 2026-10-10/11:** restart, pause and traffic-burst experiments ran live with fresh baselines, dry runs and typed approvals, and every one recovered. Predicted vs observed per SLI, ranked findings and the resilience score come from the run records ([docs/m5-results.md](docs/m5-results.md)). The 5.7 re-run showed the 3 s search timeout cuts a failing search from 10.0 s to 3.0 s. Remaining M6 work (findings report, video, submission) is tracked in [docs/day2-plan.md](docs/day2-plan.md).
- Team repository: https://github.com/adicodegiter/ditto-debugger

## Prerequisites

- Windows 10/11 with PowerShell 7 recommended, Git, Docker Desktop with Docker Compose v2, and at least 2 CPU cores and 4 GB RAM available to Docker for the upstream single-instance Ditto setup.
- Python 3.12+ for repository checks.
- Node.js 18+ only if using Ditto's official Artillery load tool. Its documented benchmark setup also lists Kafka (with topic deletion enabled) and MMock; confirm the selected scenarios and required services during preflight.

## Local Setup

### Run the target: Eclipse Ditto 3.9.8 (`deploy/ditto/`)

`deploy/ditto/` is a self-contained copy of upstream `deployment/docker` at tag `3.9.8`,
adapted as follows (see the header of `docker-compose.yml`):

- **Pinned image tags.** [`deploy/image-pins.env`](deploy/image-pins.env) is the single source of
  truth: Ditto `3.9.8`, `mongo:7.0.14` and `nginx:1.27.2-alpine`. Nothing uses `latest`. The
  compose fallbacks repeat the same values, and `tests/test_deploy.py` checks that they match.
- **Loopback-only ports.** Every published port binds to `127.0.0.1`: 8080 (API via nginx),
  8081 (gateway direct) and 27017 (MongoDB). Upstream binds to `0.0.0.0`.
- **Named MongoDB volume.** Data lives in the `ditto_mongodb_data` volume, so it survives a
  normal reset.
- **Vendored assets.** nginx's static assets (images, WoT models) are copied into `deploy/ditto/assets/`, so
  no upstream checkout is needed at runtime.

Docker Desktop needs at least 6 GB of memory, and host port 8080 must be free.

Run from the repository root (Git Bash or WSL2 on Windows):

```bash
docker compose --env-file deploy/image-pins.env -f deploy/ditto/docker-compose.yml pull
docker compose --env-file deploy/image-pins.env -f deploy/ditto/docker-compose.yml up -d
bash deploy/health_check.sh --wait 300   # per-container health + API check, exit 0 = healthy
python simulator/smoke_test.py           # policy -> thing -> update -> read back -> search
```

| Command | What it does |
|---|---|
| `bash deploy/health_check.sh [--wait N]` | Container state/health + `/health` + authenticated search |
| `bash deploy/reset.sh` | `compose down`, pull the pinned images, `up -d`, then wait for healthy. **Keeps** MongoDB data |
| `bash deploy/reset.sh --wipe-data` | Also deletes the MongoDB volume (asks for confirmation) |
| `python simulator/smoke_test.py` | 5-step end-to-end API check, exit 0 = all PASS (`--help` for options) |

For the bounded fleet workload and baseline metrics used by the Workload & Measurement role, run:

```bash
python simulator/fleet_workload.py --duration-seconds 120 --metrics-dir data/metrics
python -m monitor.cli --metrics-dir data/metrics
```

Burst traffic uses separate twins so it cannot contaminate the normal fleet's SSE freshness
measurements. The P5 traffic-burst action passes `--vehicles 16 --vehicle-offset 100`,
which targets `bosch.fleet:vehicle-101` through `vehicle-116` instead of `vehicle-01`
through `vehicle-08`.

To summarize before, during, and after phases, restrict the monitor to an ISO-8601 window:

```bash
python -m monitor.cli --metrics-dir data/metrics --run-id <RUN_ID> --from <BEFORE_START> --to <FAULT_START>
python -m monitor.cli --metrics-dir data/metrics --run-id <RUN_ID> --from <FAULT_START> --to <ROLLBACK_TIME>
python -m monitor.cli --metrics-dir data/metrics --run-id <RUN_ID> --from <ROLLBACK_TIME> --to <RECOVERY_END>
```

The workload records JSON Lines in `data/metrics/*.jsonl`:
- **Concurrent probes:** write, search and freshness each run in their own thread with their own HTTP session, so a hung probe can't stall the others. Timeouts are 5 s for writes and 3 s for search and freshness reads, so a down service shows up as fast failures.
- **Freshness via SSE (default):** each telemetry change is timed when it arrives on Ditto's SSE stream (`received_at − last_seen`). `--freshness-method poll-proxy` falls back to reading values back, and those records are labelled as such.
- **Extra files:** a local Docker resource snapshot goes to `container.jsonl`, and a per-run summary to `data/baselines/`.

**Baselines** (`python -m safety.cli baseline` or `POST /api/v1/baselines`) are built from the **most recent live workload run only**, using at most its last 5 minutes. A baseline is refused, with the reason, if:
- the run has less than **120 s** of samples
- the run stopped writing more than 60 s ago
- any SLI is unhealthy or missing

The control plane reads the same live samples for its 30 s kill-switch window, so Prometheus is optional.

- API: `http://127.0.0.1:8080/api/2/` (sample basic auth `ditto:ditto`, loopback only)
- Ditto UI: `http://127.0.0.1:8080/ui/`
- No Swagger UI. Upstream's `swagger-ui` service and `/apidoc` route were removed because they aren't needed. For
  API reference, see https://www.eclipse.dev/ditto/http-api-doc.html.

Gotchas:
- **Use `127.0.0.1`, not `localhost`.** On Windows, `localhost` tries IPv6 `::1` first and
  adds about 2 s to every new connection, which corrupts the latency SLIs.
- **Restart the file-mounting containers after a branch switch.** If `git checkout` touches
  files in `deploy/ditto/`, nginx returns `500` (`pread() ... nginx.htpasswd failed`).
  Docker Desktop keeps the old file mount, so restart those containers:
  `docker compose --env-file deploy/image-pins.env -f deploy/ditto/docker-compose.yml restart nginx`.
- **Optional upstream source.** You can clone the source for reference into the ignored
  `vendor/` folder:
  `git clone --branch 3.9.8 --depth 1 https://github.com/eclipse-ditto/ditto vendor/ditto`.

Validate this repository's contracts:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Run the workbench locally on loopback (the target and Prometheus can remain unavailable; discovery will report that honestly):

```powershell
python -m pip install -r requirements-dev.txt
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. Discovery reads `deploy/ditto/docker-compose.yml` by default (override with `DITTO_COMPOSE_FILE`). Set `PROMETHEUS_URL` to a loopback Prometheus instance once one is configured. Never bind this workbench to `0.0.0.0`.

See [deployment instructions](docs/deployment.md) for version and telemetry caveats.

## Dashboard

```bash
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Start the workload (`python simulator/fleet_workload.py --duration-seconds 900`) to see live values.

| View | Shows |
|---|---|
| **Overview** | Live SLIs for the last 30 s (write success and p95, search success, SSE freshness p95) with 5-minute sparklines, refreshed every 5 s; latest baseline; latest run; agent status |
| **Architecture** | Dependency map (`ai/dependency_map.json`): Ditto services from compose plus the workbench components. Configured, documented and manual edges are drawn differently, with SLI tags and the SLI request paths |
| **Experiments** | Capture a baseline (refusals show the reason, e.g. under 120 s); stale baselines are flagged; plan; approve or reject proposals by typing `APPROVE`/`REJECT` with your name and a reason. Approved proposals show copy-ready operator commands |
| **Runs** | Each run's timeline (inject → kill switch → rollback → recovered), kill-switch charts poll by poll against their limits, recovery check, findings, and dry-run/forced-test labels |
| **Safety** | Policy limits and allow-list, abort thresholds, watchdog log, audit trail |

The dashboard never executes a fault. Experiments run only from the operator CLI (`python -m safety.cli`), as described in [docs/m4-runbook.md](docs/m4-runbook.md). The read-only data endpoints behind the views are `/api/v1/slis`, `/api/v1/slis/series`, `/api/v1/baselines`, `/api/v1/policy`, `/api/v1/dependency-map` and `/api/v1/watchdog`.

## Demo Workflow

1. **Start the target and the workload.** Bring up the pinned Ditto stack (see Local Setup), then run the bounded fleet workload. Keep it running in its own terminal for the whole demo:
   ```bash
   python simulator/fleet_workload.py --duration-seconds 900 --metrics-dir data/metrics
   ```
2. **Capture a baseline.** With the API running (`python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000`), call `POST /api/v1/discovery/refresh`, then `POST /api/v1/baselines`. The baseline is built from the live SLI samples in `data/metrics/` and is refused unless every SLI is healthy.
3. **Plan and approve.** Call `POST /api/v1/experiments/plan` for ranked candidates (target, hypothesis, graph evidence, predicted impact, risk). Then a human records a decision with `POST /api/v1/experiments/{id}/approval`, using the candidate hash.
4. **Rehearse, then execute from the operator process.** The API never executes; its `/execute` endpoint returns 503.
   ```bash
   python -m safety.cli execute <experiment_id> --dry-run
   CHAOS_EXECUTION_ENABLED=true python -m safety.cli execute <experiment_id>
   ```
   - The dry run changes no container and does not use up the approval.
   - Before a real run, the runner checks steady state and arms the independent watchdog. During the fault it polls the kill switch every 2 s and rolls back on the first breach.
5. **Recovery and findings.** After rollback, the runner checks `/health`, an authenticated canary, and the SLIs since rollback. It saves the run together with deterministic, evidence-linked findings (`GET /api/v1/runs`, `GET /api/v1/findings`).

More detail is in [the demo runbook](docs/hackathon-demo.md) and [the safety model](docs/safety-model.md).

## End to end: from a fresh clone to ranked findings

Run everything from the repository root (Git Bash or WSL2 on Windows). Each step says what "good" looks like.

1. **Install and test.** `python -m pip install -r requirements-dev.txt`, then
   `python -m unittest discover -s tests` (all OK).
2. **Start the target.** `docker compose --env-file deploy/image-pins.env -f deploy/ditto/docker-compose.yml up -d`,
   then `bash deploy/health_check.sh --wait 300` (OVERALL: HEALTHY) and `python simulator/smoke_test.py` (5/5 PASS).
3. **Start the workload** in its own terminal and leave it running for the whole session:
   `python simulator/fleet_workload.py --duration-seconds 1800 --metrics-dir data/metrics`.
   Wait **at least 120 s** before the next step.
4. **Optional: dashboard.** `python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000`, then open
   http://127.0.0.1:8000.
5. **Baseline:** `python -m safety.cli baseline`. It is refused, with the reason, if the run has under 120 s of
   samples or any SLI is unhealthy. After a fault, wait about 5 minutes before the next baseline so the fault is
   outside its 5-minute window.
6. **Plan:** `python -m safety.cli plan <baseline_id>`. Each candidate shows its action (`restart_container`,
   `stop_container`, `pause_container`, `cpu_throttle`, `traffic_burst`), target, duration, hypothesis and per-SLI prediction.
7. **Approve (a human types `APPROVE`):** `python -m safety.cli approve <experiment_id> --by "<name>" --reason "<why>"`.
   The approval is bound to that candidate, expires after 10 minutes and works for one real run.
8. **Rehearse, then run:**
   ```bash
   python -m safety.cli execute <experiment_id> --dry-run
   CHAOS_EXECUTION_ENABLED=true python -m safety.cli execute <experiment_id>
   ```
   The dry run changes no container and prints the exact commands. The real run arms the watchdog, polls the kill
   switch every 2 s, rolls back, and checks recovery.
9. **Results:** the run record is saved to `data/experiments/<run_id>.json` with its diagnosis (predicted vs observed),
   findings and resilience score (`score_components`, `score_inputs`). Rank findings across runs with
   `python -m ai.ranking --markdown --run-file <run file> [--run-file ...]`.

Where the data goes (all git-ignored, created on the machine that runs the stack):

| Folder | Written by |
|---|---|
| `data/metrics/` | the fleet workload: `write`, `search`, `freshness`, `container` JSON Lines |
| `data/metrics-burst/` | a `traffic_burst` experiment's own bounded workload |
| `data/baselines/` | `safety.cli baseline` |
| `data/experiments/` | `safety.cli execute`: one record per run (`-dry-run` / `-forced-test` suffixes), plus `watchdog.jsonl` |

**Recorded results.** The official runs on the Windows host are copied to
[docs/evidence/windows-host/](docs/evidence/windows-host/README.md) and summarized in
[docs/m4-results.md](docs/m4-results.md) and [docs/m5-results.md](docs/m5-results.md): `things-search` restart and
pause, a traffic burst through nginx, and the 5.7 remediation re-run. Forced kill-switch tests (`--forced-test`) are
labelled and never scored. Deliverables: [architecture](docs/architecture.md), findings report
(`docs/findings-report.md`, in progress), demo video (link to follow).

## Optional: Groq for finding text (Agent 3)

The findings are deterministic and work with no key. To have Groq rewrite each finding's summary and recommendation:

1. `cp .env.example .env` (the real `.env` is git-ignored; never commit it or put the key in `.env.example`).
2. In `.env` set `AI_PROVIDER=groq`, paste your key after `GROQ_API_KEY=`, and set `GROQ_MODEL` to a model id from Groq's current list.
3. Check it: `python -m ai.llm` sends one tiny request (no run data) and prints OK or the failure.

Groq only rewrites the text. Ids, severity, evidence and confidence stay deterministic, dry runs and forced tests are never sent, and any API error, rate limit or malformed reply falls back to the deterministic findings. It has no tools and cannot execute anything.

## Team Workflow

Start with [CONTRIBUTING.md](CONTRIBUTING.md), use the issue forms under `.github/ISSUE_TEMPLATE/`, and make pull requests using `.github/pull_request_template.md`. Work is divided by module ownership and acceptance criteria in [the project blueprint](docs/project-blueprint.md). Do not commit `.env` files, credentials, private logs, Ditto data, or the external upstream checkout.

## License and Authorization

Original project files use the MIT License; Eclipse Ditto remains separately licensed under EPL-2.0. Confirm Bosch hackathon and employer IP/publication rules before public release. Use this tool only on systems you own or are explicitly authorized to test. See [SECURITY.md](SECURITY.md).