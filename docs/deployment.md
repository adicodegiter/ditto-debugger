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
python -m unittest discover -s tests        # 273 tests; 1 LangGraph-only test is skipped without it
python -m ai.dependency_map --check         # dependency map up to date with the compose file
```

## Deploying to Render (split frontend + backend)

The workbench can run as two Render services: a **static site** for the dashboard and a
**Python web service** for the API. See [`render.yaml`](../render.yaml) for a ready-made
Blueprint, or set the equivalent values manually in the Render dashboard.

**Backend (`ditto-debugger-api`, type: Web Service, runtime: Python)**

| Setting | Value |
|---|---|
| Root directory | `.` |
| Build command | `pip install -r requirements.txt` |
| Start command | `PYTHONPATH=. python -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port $PORT` |
| Health check path | `/healthz` |

The two production blockers this fixes:
- **Binding.** Render can only reach a service that listens on `0.0.0.0` and the assigned
  `$PORT`. The local `--host 127.0.0.1 --port 8000` command is unreachable from Render's edge,
  which is why the backend URL returned **503**. `PYTHONPATH=.` also puts the repo-root packages
  (`safety`, `monitor`, `ai`, `tools`) on the import path; `--app-dir backend` puts `app.*` there.
- **CORS.** The dashboard is served from a different origin than the API, so `backend/app/main.py`
  now sends `Access-Control-Allow-Origin` on every response (including errors) and answers the
  `OPTIONS` preflight. Without it the browser blocks every `fetch()` and the UI hangs on "Loading…".

**Frontend (`ditto-debugger`, type: Static Site)**

| Setting | Value |
|---|---|
| Root directory | `.` |
| Publish directory | `frontend` |
| Build command | *(none — plain HTML/CSS/JS)* |

`frontend/config.js` (loaded before `app.js`/`lab.js`) is the single override point for the API
base URL. It defaults `window.DITTO_API_BASE` to `https://ditto-debugger1.onrender.com`; if unset,
the scripts fall back to same-origin on `localhost`/`127.0.0.1` (so a locally-run API keeps working)
and to the deployed backend anywhere else.

**Environment variables you may set on the backend (dashboard → Environment):**
- `CORS_ALLOWED_ORIGINS` — comma-separated browser origins (defaults to the frontend above plus the
  local dev origins). Keep it in sync with the final frontend URL and any custom/preview domains.
- `CHAOS_EXECUTION_ENABLED` — leave `false` (default). Execution stays in the operator CLI.
- `AUTONOMY_ENABLED` — set `true` to run the three advisory agents (discover -> measure -> plan) on a
  schedule so a fresh, ranked candidate set is always ready for a human. Advisory only: the loop never
  approves or executes; faults still run only from the operator CLI after typed approval.
  `AUTONOMY_INTERVAL_SECONDS` (default `60`) sets the cadence. Inspect it with `GET /api/v1/autonomy`.
- `DITTO_BASE_URL`, `PROMETHEUS_URL` — **optional.** Do **not** point these at loopback; they cannot
  be reached from Render. With them unset the backend still serves health, overview, component status,
  experiments, runs and safety from the committed manifests and any recorded data, and it reports
  Ditto/Prometheus as unavailable rather than pretending they are connected. No secrets belong in the
  repo; set any real keys in the dashboard, never in `render.yaml` or source.

After changing the start command or env vars, trigger a manual deploy (Render does not always pick up
start-command edits on its own). Verify with: `curl -i https://<api>/healthz` (expect `200` and an
`access-control-allow-origin` header on an `Origin` request) and `curl -i https://<api>/api/v1/health`.


## Release / publication

The team repository is https://github.com/adicodegiter/ditto-debugger. GitHub Actions validates the contracts.
Before any wider release, confirm Bosch hackathon and employer rules on authorship and publication, keep all
images and dependencies pinned, and run a dependency CVE and license check.
