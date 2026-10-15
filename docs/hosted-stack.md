# Hosting the live Ditto stack (Option C)

This is the guide for a **self-sufficient, live** deployment: the dashboard, the autonomous advisory
agents, live telemetry, and a real fault target. It is the honest, more involved alternative to the
simple two-service setup in [`deployment.md`](deployment.md).

**Why you're seeing "Baseline refused: No live workload samples found":** a baseline (and any live
telemetry) requires the fleet workload to be running against a **reachable Ditto**. On Render there
is no Ditto, so the workload has nothing to talk to and no samples are produced. That refusal is the
system being honest — it will not fabricate a healthy baseline. To fix it you must stand up the live
chain below. There are two ways; the first is strongly recommended.

## Recommended: run the whole Ditto stack on ONE host

Your Ditto stack is a **Pekko/Akka cluster** that is happiest on a single Docker network. Instead of
splitting it across ~9 Render services (where cross-service clustering is fragile), run the entire
stack + Prometheus on **one** host and point the Render backend at it.

```bash
# From deploy/ditto/, on any single host Render can reach (Fly.io machine, Railway, a VPS,
# a Render Docker/web service, or your laptop for a local demo):
docker compose --env-file ../image-pins.env \
  -f docker-compose.yml -f docker-compose.hosted.yml up -d
```

`docker-compose.hosted.yml` publishes the nginx gateway on `0.0.0.0:8080` and adds Prometheus
(`0.0.0.0:9090`), all on one network so clustering and service DNS just work. Then set these on the
**Render backend** (dashboard → Environment) and redeploy:

```
DITTO_BASE_URL          = https://<your-ditto-host>       # the nginx gateway
DITTO_USERNAME          = ditto                           # must match nginx pre-auth
DITTO_PASSWORD          = ditto
PROMETHEUS_URL          = https://<your-ditto-host>:9090   # or the host's private address
PROMETHEUS_ALLOW_REMOTE = true
WORKLOAD_ENABLED        = true                             # backend runs the fleet workload itself
```

Once the gateway is up and the workload has run for **≥120 s with every SLI healthy**, the Overview
"Capture baseline" button will succeed and the live chip turns green. Verify with:

```bash
curl -i https://ditto-debugger1.onrender.com/api/v1/workload    # last_status: healthy
curl -i https://ditto-debugger1.onrender.com/api/v1/telemetry   # status: available/degraded
```

> **Security:** the hosted override publishes the gateway with the sample `ditto:ditto` pre-auth.
> Put TLS + real auth (or a private network / firewall) in front before exposing it publicly, and
> never reuse the sample credentials in production. `CHAOS_EXECUTION_ENABLED` stays `false` — the
> workload only generates healthy twin traffic and never injects a fault.

## Alternative: fully on Render (advanced, needs paid tiers)

<details><summary>Expand the multi-service Render approach</summary>

> **Read this first.** Your Ditto stack is **8 containers that form a Pekko/Akka cluster**. Render
> does **not** run `docker-compose`, so each container becomes its own Render service. This needs
> **paid Render tiers** and **manual iteration**. None of it can be tested from here — deploy
> incrementally and read the logs at each step. Prefer the single-host option above when you can.

The dashboard's "live samples" come from `data/metrics/*.jsonl`, which the **fleet workload** writes,
and the backend reads. Two consequences drove the design:

1. **The workload runs inside the backend service.** On Render the API and the workload must share one
   filesystem, so `WORKLOAD_ENABLED=true` starts the workload as a background thread in the API
   process (reusing `simulator/fleet_workload.py`). It writes to the same `metrics_dir` the API reads.
2. **The workload needs a reachable Ditto.** It PUTs twins and probes write/search/freshness. With no
   Ditto it records an honest error (`GET /api/v1/workload`) and retries — it never fakes samples.

The full chain: **Ditto (twins + /metrics) → Prometheus (scrapes :9095) → backend (queries Prometheus
for the Measure step, and runs the workload for live SLIs) → dashboard.**

## The blueprint

[`render-stack.yaml`](../render-stack.yaml) defines 9 services. Create it as a **new Blueprint**
(New + → Blueprint), or use it as the reference for manual service setup. Expected monthly cost on
paid tiers is roughly **$50-150** depending on plans (8 paid services + a disk).

| Service | Type | Plan | Notes |
|---|---|---|---|
| `ditto-mongo` | private | starter + disk | MongoDB 7.0.14, persistent disk `ditto-mongo` |
| `ditto-policies` | private | starter | Ditto 3.9.8 JVM |
| `ditto-things` | private | starter | |
| `ditto-things-search` | private | starter | |
| `ditto-connectivity` | private | starter | |
| `ditto-gateway` | web | starter | Ditto API entry point (pre-auth on) |
| `ditto-prometheus` | private | starter | needs a wrapper image (below) |
| `ditto-debugger1` | web | starter | the workbench API (+ workload + autonomy) |
| `ditto-debugger` | static | free | the dashboard |

## Step 1 — Prometheus wrapper image

Render cannot mount a loose file into a Docker service, so bake
[`deploy/prometheus/prometheus.yml`](../deploy/prometheus/prometheus.yml) into an image. Create
`deploy/prometheus/Dockerfile`:

```dockerfile
FROM docker.io/prom/prometheus:latest
COPY prometheus.yml /etc/prometheus/prometheus.yml
```

Build and push it (Docker Hub), then set `dockerImagePath` in `render-stack.yaml` to your image:

```bash
docker build -t your-dockerhub-user/ditto-prometheus:latest deploy/prometheus
docker push your-dockerhub-user/ditto-prometheus:latest
```

In `prometheus.yml`, set the scrape targets to your actual Render service names/hosts (the Ditto
services each expose metrics on `:9095`).

## Step 2 — Deploy order

1. **`ditto-mongo`** first (create its disk in the dashboard first). Wait until it's live.
2. **The five microservices** (`policies`, `things`, `things-search`, `connectivity`, `gateway`).
3. **`ditto-prometheus`** (your wrapper image).
4. **`ditto-debugger1`** (the workbench) — set its `DITTO_BASE_URL`/`PROMETHEUS_URL` to the internal
   hostnames of the gateway and Prometheus.
5. **`ditto-debugger`** (the static dashboard).

## Step 3 — Verify each layer

```bash
# Ditto gateway answers?
curl -i http://ditto-gateway:8080/api/2/things        # from another Render service, or its public URL

# Prometheus has targets?
curl -s "http://ditto-prometheus:9090/api/v1/query?query=up"

# Workbench telemetry now available?
curl -i https://ditto-debugger1.onrender.com/api/v1/telemetry   # status: available/degraded

# Workload producing live SLIs?
curl -i https://ditto-debugger1.onrender.com/api/v1/workload    # last_status: healthy

# Dashboard live chip should turn green once samples are < 15s old.
```

## The fiddly part: Pekko clustering

Ditto's services discover each other via a Pekko/Akka cluster. Across separate Render services the
**seed-node hostnames** (`-Dpekko.cluster.seed-names=...` in `render-stack.yaml`) must resolve to the
other services' Render internal addresses. This is the most likely thing to need adjustment:

- Verify the exact clustering system properties for **Ditto 3.9.8** against the official
  "Operating Ditto" documentation.
- Confirm Render's private-service DNS for your region (`<name>` in Blueprints, `<name>.internal` in
  some setups) and match the seed names to it.
- Watch the `ditto-*` service logs for cluster join/leave messages; a service that never joins the
  cluster will report unhealthy.

## Security notes

- The gateway uses **pre-authentication** (`ENABLE_PRE_AUTHENTICATION=true`) with the sample
  `ditto:ditto` credentials, exactly like the local stack. For anything public, change the devops
  password and put real auth in front — **never** rely on the sample creds in production.
- Put auth in front of Prometheus; this app does not authenticate to it.
- No secrets belong in `render-stack.yaml` — set real values in the Render dashboard.
- **`CHAOS_EXECUTION_ENABLED` stays `false`.** Execution remains in the operator CLI behind typed
  human approval, even with a live target. The autonomous loop and the workload never inject faults.
