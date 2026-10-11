# Tools, Libraries and AI Models

Running list for the team submission form. Add an entry whenever you introduce something.

## Application under test
| Item | Version | Purpose | License |
|---|---|---|---|
| Eclipse Ditto (policies, things, things-search, connectivity, gateway, ditto-ui) | 3.9.8 | Digital-twin platform under test | EPL-2.0 |
| MongoDB | 7.0.14 | Ditto persistence | SSPL |
| nginx | 1.27.2-alpine | Ditto reverse proxy / basic auth | BSD-2-Clause |

## Runtime and tooling
| Item | Version | Purpose |
|---|---|---|
| Docker Desktop / Docker Engine | 24.0.6 | Runs the target stack |
| Docker Compose | v2.23.0 | Stack orchestration |
| Python | 3.12.4 (3.11+ supported) | Backend, agents, workload, probes |
| Git | 2.39 | Version control |
| GitHub | – | Team repo, pull requests |

## Python libraries
| Library | Purpose |
|---|---|
| FastAPI 0.116, uvicorn 0.36 | Workbench API (loopback only) |
| PyYAML 6.0 | Compose/config parsing |
| requests 2.32 | Workload generator, probes, smoke test |
| httpx 0.27, jsonschema 4.23 | Tests and contract validation (dev) |
| LangGraph 1.2.12 (optional, `requirements-graph.txt`) | Orchestrates the three advisory agents as a state graph with gate edges and a human-approval interrupt (`backend/app/graph.py`); falls back to sequential calls when not installed. Never executes faults |
| Python standard library (`sqlite3`, `unittest`, `subprocess`, `threading`) | Run/approval store, tests, fixed Docker commands, concurrent probes |

The dashboard (`frontend/`) is plain HTML, CSS and JavaScript with no third-party libraries.

## AI models
| Model | Where used | Notes |
|---|---|---|
| Groq API (`GROQ_MODEL`, default `openai/gpt-oss-120b`) | `ai/explain.py` rewrites finding summary/recommendation when `AI_PROVIDER=groq` | No tools, no execution; output schema-checked, falls back to the deterministic findings without a key or on any API error |

## AI coding assistants used during development
| Tool | Purpose |
|---|---|
| Claude Code (Claude Opus 5.5, Claude Sonnet 5.5) | Scaffolding, Ditto deployment, scripts, tests, reviews, docs |
| GitHub Copilot | Code suggestions and PR work |

## Data used
| Data | Source | Notes |
|---|---|---|
| Fleet telemetry (vehicle twins with `speed_kph` and `last_seen`) | Generated locally by `simulator/fleet_workload.py` | Synthetic; no real vehicles, no personal data |
| SLI samples, baselines, run records | Produced by our own probes and runner (`data/metrics/`, `data/baselines/`, `data/experiments/`) | Local only, gitignored |
| Ditto deployment files and nginx assets | Upstream Eclipse Ditto 3.9.8 `deployment/docker` (vendored in `deploy/ditto/`) | EPL-2.0; sample basic-auth user `ditto:ditto`, loopback only |

No external datasets are used, and the stack under test uses no cloud services or public endpoints. The one optional external call is the Groq API for finding text, off unless `AI_PROVIDER=groq`.
