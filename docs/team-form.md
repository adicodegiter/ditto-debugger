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
Roles are inferred from each person's commits and PRs (GitHub handle in brackets). **Each person
confirms their name and role, and adds their full name where only a handle is shown, before P1
submits.**

| Role | Area | Member (from git history) | Confirmed |
|---|---|---|---|
| P1 | Ditto deploy, discovery (Agent 1), integration and merges | Radhika Kumar (radhikakumar0705) | [ ] |
| P2 | Mapping and graph, schemas, API, dashboard, architecture page | adicodegiter: P2 changelog and the workbench foundation | [ ] |
| P3 | Workload and measurement (Agent 2), SSE freshness, baseline, demo video | Varikuti Mahathi Reddy (MahathiVarikuti): P3 workload, SSE freshness, burst isolation | [ ] |
| P4 | Planner and analyst (Agent 3), diagnosis, findings report | Bhoomika Babu (bhoomikab0b0-droid): P4 planner diagnosis (#5, #17) | [ ] |
| P5 | Control plane: policy, runner, kill switch, rollback, resilience score | SIDR1921: P5 control plane, scoring (#15, #19), ranked findings (#16), Groq adapter | [ ] |

## Tools
Docker Desktop 24.0.6 with Docker Compose v2.23 (WSL2, Windows 11), Python 3.12, Git, GitHub.

## AI models
- **In the product (optional):** Groq API, model `openai/gpt-oss-120b` by default (`GROQ_MODEL`), used
  only by `ai/explain.py` to reword a finding's summary and recommendation. It is **off by default**
  (`AI_PROVIDER=none`): with no key, on any API error or past the per-process call cap, the deterministic
  findings are used. It has no tools and cannot execute anything; ids, severity, evidence and confidence
  stay deterministic, and its output is schema-checked. The planner, diagnosis, ranking and score are
  always deterministic.
- **During development:** Claude Code (Claude Opus 5.5 and Claude Sonnet 5.5) and GitHub Copilot, for
  scaffolding, deployment, scripts, tests, reviews and documentation.

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
records our own tools produce. No external datasets and no personal data. The stack under test
and all experiments run on loopback on the team's Windows host.

The only data that can leave the machine: when someone sets `AI_PROVIDER=groq`, a run's findings
(id, severity, summary, evidence references, recommendation), its diagnosis (predicted vs observed
SLIs) and its abort and recovery status are sent to the Groq API to be reworded. It is off by
default, and the recorded Windows-host runs pre-date the adapter, so their findings are the
deterministic ones.
