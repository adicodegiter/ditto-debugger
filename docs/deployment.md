# Deployment and Local Development

Everything runs on one machine, on loopback only. There are two processes besides the Ditto stack: the
workbench API/dashboard and, only when a human has approved an experiment, the operator CLI.

## Ditto stack (application under test)

`deploy/ditto/` is a pinned copy of upstream `deployment/docker` at Ditto `3.9.8`. Images are pinned in
[`deploy/image-pins.env`](../deploy/image-pins.env) (Ditto `3.9.8`, `mongo:7.0.14`, `nginx:1.27.2-alpine`; never
`latest`), and every published port binds to `127.0.0.1` (8080 API via nginx, 8081 gateway, 27017 MongoDB).

```bash
docker compose --env-file deploy/image-pins.env -f deploy/ditto/docker-compose.yml up -d
bash deploy/health_check.sh --wait 300   # OVERALL: HEALTHY
python simulator/smoke_test.py           # 5/5 PASS
bash deploy/reset.sh                     # down, pull pinned images, up, wait; keeps MongoDB data
```

Docker Desktop needs at least 6 GB of memory, and host port 8080 must be free. On Windows run the shell scripts
from Git Bash or WSL2. `bash deploy/reset.sh --wipe-data` also deletes the MongoDB volume and asks first.

## Workbench (API, dashboard, agents)

```bash
python -m pip install -r requirements-dev.txt          # add -r requirements-graph.txt for the optional LangGraph agent graph
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. Never bind the workbench to `0.0.0.0`. Runtime data goes to `data/` (git-ignored):
SLI samples in `data/metrics/*.jsonl`, baselines in `data/baselines/`, run records in `data/experiments/`. The SQLite store
(plans, approvals, runs, findings, audit) defaults to `.local/workbench.sqlite3` (also git-ignored).

## Environment and secrets

`.env.example` holds the endpoint and AI-provider defaults; copy it to `.env` (git-ignored) for local overrides.
The other settings are read from the environment (`backend/app/settings.py`). Key settings:

| Variable | Default | Purpose |
|---|---|---|
| `DITTO_BASE_URL` | `http://127.0.0.1:8080` | Ditto API. Use `127.0.0.1`, not `localhost` (IPv6 lookup adds ~2 s per connection on Windows) |
| `METRICS_DIR` | `data/metrics` | Where probes write and the kill switch reads SLI samples |
| `CHAOS_EXECUTION_ENABLED` | unset (false) | Must be `true` for the operator CLI to change a container |
| `AI_PROVIDER`, `GROQ_API_KEY`, `GROQ_MODEL` | `none` | Optional Groq rewording of finding text |
| `PROMETHEUS_URL` | `http://127.0.0.1:9090` | Optional; the kill switch and baselines use the JSON Lines samples |
| `DATABASE_PATH` | `.local/workbench.sqlite3` | Workbench store |
| `EXPERIMENT_POLICY_FILE` | `configs/experiment-policy.yaml` | Policy (allowlist, fault catalog, thresholds) |
| `DITTO_USER`, `DITTO_PASS` | `ditto` / `ditto` | Basic auth for the loopback sample stack |

Never commit `.env`, keys or the upstream `nginx.htpasswd`. The sample basic-auth user `ditto:ditto` is for the
loopback stack only.

## Development checks

```bash
python -m unittest discover -s tests        # 263 tests; 1 LangGraph-only test is skipped without it
python -m ai.dependency_map --check         # dependency map up to date with the compose file
```

## Release / publication

The team repository is https://github.com/adicodegiter/ditto-debugger. GitHub Actions validates the contracts.
Before any wider release, confirm Bosch hackathon and employer rules on authorship and publication, keep all
images and dependencies pinned, and run a dependency CVE and license check.
