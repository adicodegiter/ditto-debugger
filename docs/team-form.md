# Team form (task 6.7): draft answers

Copy these into the hackathon team form. P1 submits it; everyone checks their own row first.
Tools, libraries and data come from [TOOLS.md](../TOOLS.md), which is the source of truth.

## Project
- **Name:** Ditto Resilience Workbench
- **One line:** AI-driven chaos engineering for a connected-vehicle digital-twin backend,
  built on Eclipse Ditto (a Bosch-originated platform).
- **Repository:** https://github.com/adicodegiter/ditto-debugger
- **Demo video:** _link after 6.6_

## Members
Names are from the repository's commit history. Each person confirms their name and role
before submission.

| Role | Area | Member | Confirmed |
|---|---|---|---|
| P1 | Ditto deploy, discovery (Agent 1), integration and merges | Radhika Kumar | [ ] |
| P2 | Mapping and graph, schemas, API, dashboard, architecture page | _to confirm_ | [ ] |
| P3 | Workload and measurement (Agent 2), SSE freshness, baseline, demo video | _to confirm_ | [ ] |
| P4 | Planner and analyst (Agent 3), diagnosis, findings report | _to confirm_ | [ ] |
| P5 | Control plane: policy, runner, kill switch, rollback, resilience score | _to confirm_ | [ ] |

Other contributors in the git history: Varikuti Mahathi Reddy, adicodegiter,
Bhoomika Babu, SIDR1921.

## Tools
Docker Desktop 24.0.6 with Docker Compose v2.23 (WSL2, Windows 11), Python 3.12, Git, GitHub.

## AI models
- **In the product:** none. The planner, diagnosis and findings are deterministic
  (`AI_PROVIDER=none`); every LLM feature must keep this no-API-key fallback.
- **During development:** Claude Code (Claude Opus 5.5) for scaffolding, deployment,
  scripts and documentation.

## Third-party libraries
| Library | License | Use |
|---|---|---|
| FastAPI | MIT | Workbench API |
| uvicorn | BSD-3-Clause | ASGI server |
| PyYAML | MIT | Compose and policy parsing |
| requests | Apache-2.0 | Workload, probes, smoke test |
| httpx (dev) | BSD-3-Clause | API tests |
| jsonschema (dev) | MIT | Contract validation |

Application under test: Eclipse Ditto 3.9.8 (EPL-2.0), MongoDB 7.0.14 (SSPL),
nginx 1.27.2-alpine (BSD-2-Clause). Dashboard: plain HTML/CSS/JS, no libraries.

## Data used
Synthetic fleet telemetry generated locally by `simulator/fleet_workload.py`
(vehicle twins with `speed_kph` and `last_seen`), plus the SLI samples, baselines and run
records our own tools produce. No external datasets, no personal data, no cloud services.
Everything runs on loopback on the team's Windows host.
