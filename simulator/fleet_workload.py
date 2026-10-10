#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor.sli import append_jsonl, baseline_summary, load_thresholds, utc_timestamp  # noqa: E402
from monitor.container import capture_container_metrics  # noqa: E402
from monitor.sse import FreshnessSseSubscriber  # noqa: E402

# Short timeouts so a down service shows up as fast failures, not hangs. Each probe also runs
# in its own thread with its own HTTP session, so one hung probe cannot stall the others
# (in the M4 run a 10 s search hang blocked writes and inflated freshness).
WRITE_TIMEOUT_S = 5.0
SEARCH_TIMEOUT_S = 3.0
FRESHNESS_TIMEOUT_S = 3.0
from simulator.config import AUTH, DITTO_BASE_URL, NAMESPACE, POLICY_ID, policy_body  # noqa: E402


def _safe_json(response: requests.Response) -> dict:
    try:
        return response.json()
    except ValueError:
        return {}


def _resolve_api_base(base_url: str) -> str:
    value = base_url.rstrip("/")
    return value if value.endswith("/api/2") else f"{value}/api/2"


def _failed_probe(kind: str, vehicle_id: str, started: float, error: Exception, run_id: str) -> dict:
    return {
        "kind": kind,
        "source": "live",
        "run_id": run_id,
        "vehicle_id": vehicle_id,
        "success": False,
        "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "status_code": None,
        "error": type(error).__name__,
        "timestamp": utc_timestamp(),
    }


def _run_write_probe(session: requests.Session, vehicle_id: str, *, value: int, api_base: str, run_id: str) -> dict:
    started = time.perf_counter()
    payload = {"speed_kph": value, "last_seen": utc_timestamp()}
    try:
        response = session.put(
            f"{api_base}/things/{vehicle_id}/features/telemetry/properties",
            json=payload,
            timeout=WRITE_TIMEOUT_S,
        )
    except requests.RequestException as error:
        return _failed_probe("write", vehicle_id, started, error, run_id)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return {
        "kind": "write",
        "source": "live",
        "run_id": run_id,
        "vehicle_id": vehicle_id,
        "success": response.status_code in {200, 201, 204},
        "latency_ms": round(elapsed_ms, 3),
        "status_code": response.status_code,
        "timestamp": utc_timestamp(),
    }


def _ensure_vehicle(session: requests.Session, vehicle_id: str, api_base: str) -> None:
    payload = {
        "policyId": POLICY_ID,
        "attributes": {"model": "fleet", "fleet": "bosch"},
        "features": {"telemetry": {"properties": {"speed_kph": 0, "last_seen": utc_timestamp()}}},
    }
    response = session.put(f"{api_base}/things/{vehicle_id}", json=payload, timeout=10)
    if response.status_code not in {200, 201, 204}:
        raise RuntimeError(f"Unable to bootstrap {vehicle_id} ({response.status_code}): {response.text}")


def _run_search_probe(session: requests.Session, vehicle_id: str, *, api_base: str, run_id: str) -> dict:
    started = time.perf_counter()
    search = f'and(eq(thingId,"{vehicle_id}"),gt(features/telemetry/properties/speed_kph,0))'
    try:
        response = session.get(f"{api_base}/search/things", params={"filter": search}, timeout=SEARCH_TIMEOUT_S)
    except requests.RequestException as error:
        return _failed_probe("search", vehicle_id, started, error, run_id)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    payload = _safe_json(response)
    return {
        "kind": "search",
        "source": "live",
        "run_id": run_id,
        "vehicle_id": vehicle_id,
        "success": response.status_code == 200,
        "expected_hit": bool(payload.get("items")),
        "latency_ms": round(elapsed_ms, 3),
        "status_code": response.status_code,
        "items": len(payload.get("items", [])),
        "timestamp": utc_timestamp(),
    }


def _run_freshness_probe(session: requests.Session, vehicle_id: str, *, api_base: str, run_id: str) -> dict:
    started = time.perf_counter()
    try:
        response = session.get(f"{api_base}/things/{vehicle_id}/features/telemetry/properties/last_seen", timeout=FRESHNESS_TIMEOUT_S)
    except requests.RequestException as error:
        return _failed_probe("freshness", vehicle_id, started, error, run_id)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    payload = _safe_json(response)
    freshness_seconds = None
    if response.status_code == 200 and isinstance(payload, (int, float, str)):
        try:
            observed_at = datetime.fromisoformat(str(payload).replace("Z", "+00:00"))
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            freshness_seconds = max(0.0, (datetime.now(timezone.utc) - observed_at.astimezone(timezone.utc)).total_seconds())
        except ValueError:
            freshness_seconds = None
    return {
        "kind": "freshness",
        "source": "live",
        "run_id": run_id,
        "method": "poll-proxy",
        "vehicle_id": vehicle_id,
        "success": response.status_code == 200 and freshness_seconds is not None,
        "observed_at": payload if isinstance(payload, str) else utc_timestamp(),
        "freshness_seconds": freshness_seconds,
        "latency_ms": round(elapsed_ms, 3),
        "timestamp": utc_timestamp(),
    }


def _run_sse_freshness_probe(subscriber: FreshnessSseSubscriber, vehicle_id: str, run_id: str) -> dict:
    started = time.perf_counter()
    sample = subscriber.sample(vehicle_id)
    return {
        "kind": "freshness",
        "source": "live",
        "run_id": run_id,
        "method": "sse",
        "vehicle_id": vehicle_id,
        **sample,
        "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "status_code": 200 if sample["success"] else None,
        "timestamp": utc_timestamp(),
    }


def run_fleet_workload(*, base_url: str = DITTO_BASE_URL, vehicles: int = 8, vehicle_offset: int = 0, duration_seconds: float = 120.0, rate_per_second: float = 2.0, metrics_dir: str | Path = "data/metrics", capture_containers: bool = True, freshness_method: str = "sse") -> dict:
    if vehicle_offset < 0 or vehicle_offset + vehicles > 999:
        raise ValueError("vehicle_offset must be >= 0 and offset + vehicles <= 999")
    metrics_path = Path(metrics_dir)
    metrics_path.mkdir(parents=True, exist_ok=True)
    api_base = _resolve_api_base(base_url)
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    session = requests.Session()
    session.auth = AUTH
    if capture_containers:
        capture_container_metrics(metrics_path)

    policy_response = session.put(f"{api_base}/policies/{POLICY_ID}", json=policy_body(), timeout=10)
    if policy_response.status_code not in {200, 201, 204}:
        raise RuntimeError(f"Unable to create the fleet policy ({policy_response.status_code}): {policy_response.text}")

    deadline = time.monotonic() + float(duration_seconds)
    vehicle_ids = [
        f"{NAMESPACE}:vehicle-{idx:02d}"
        for idx in range(vehicle_offset + 1, vehicle_offset + vehicles + 1)
    ]
    for vehicle_id in vehicle_ids:
        _ensure_vehicle(session, vehicle_id, api_base)
    stop = threading.Event()
    interval = 1.0 / max(rate_per_second, 0.1)

    def new_session() -> requests.Session:
        probe_session = requests.Session()
        probe_session.auth = AUTH
        return probe_session

    def loop(name: str, probe) -> None:
        probe_session = new_session()
        while not stop.is_set() and time.monotonic() < deadline:
            for vehicle_id in vehicle_ids:
                if stop.is_set() or time.monotonic() >= deadline:
                    return
                probe(probe_session, vehicle_id)
                stop.wait(interval)

    def write(probe_session: requests.Session, vehicle_id: str) -> None:
        value = int((time.monotonic() * 1000) % 100) + 1
        append_jsonl(metrics_path / "write.jsonl", _run_write_probe(probe_session, vehicle_id, value=value, api_base=api_base, run_id=run_id))

    def search(probe_session: requests.Session, vehicle_id: str) -> None:
        append_jsonl(metrics_path / "search.jsonl", _run_search_probe(probe_session, vehicle_id, api_base=api_base, run_id=run_id))

    def poll_freshness(probe_session: requests.Session, vehicle_id: str) -> None:
        append_jsonl(metrics_path / "freshness.jsonl", _run_freshness_probe(probe_session, vehicle_id, api_base=api_base, run_id=run_id))

    def on_sse_event(event: dict) -> None:
        # Only the subscriber thread writes freshness.jsonl in SSE mode.
        append_jsonl(metrics_path / "freshness.jsonl", {
            "kind": "freshness", "source": "live", "run_id": run_id, "method": "sse",
            "timestamp": event.get("received_at") or utc_timestamp(), **event,
        })

    subscriber = FreshnessSseSubscriber(api_base, vehicle_ids, AUTH, on_event=on_sse_event) if freshness_method == "sse" else None
    threads = [threading.Thread(target=loop, args=("write", write), name="probe-write", daemon=True),
               threading.Thread(target=loop, args=("search", search), name="probe-search", daemon=True)]
    if subscriber is None:
        threads.append(threading.Thread(target=loop, args=("freshness", poll_freshness), name="probe-freshness", daemon=True))
    else:
        subscriber.start()
    for thread in threads:
        thread.start()
    try:
        while any(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(timeout=0.5)
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=WRITE_TIMEOUT_S + 1)
        if subscriber is not None:
            subscriber.close()

    thresholds_path = Path(__file__).resolve().parents[1] / "configs" / "telemetry.yaml"
    summary = baseline_summary(metrics_path, run_id=run_id, thresholds=load_thresholds(thresholds_path))
    summary["run"] = {
        "run_id": run_id,
        "base_url": base_url,
        "vehicles": vehicles,
        "vehicle_offset": vehicle_offset,
        "duration_seconds": duration_seconds,
        "rate_per_second": rate_per_second,
        "captured_at": utc_timestamp(),
    }
    baseline_id = f"baseline-{run_id}"
    baseline = {
        "schema_version": "1.0",
        "baseline_id": baseline_id,
        "run_id": run_id,
        "captured_at": summary["run"]["captured_at"],
        "source": "client-sli-jsonl",
        "status": summary["status"],
        "summary": summary,
        "metric_files": [
            str(metrics_path / name)
            for name in ("write.jsonl", "search.jsonl", "freshness.jsonl", "container.jsonl")
            if (metrics_path / name).exists()
        ],
    }
    baseline_dir = metrics_path.parent / "baselines"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    (baseline_dir / f"{baseline_id}.json").write_text(json.dumps(baseline, indent=2, sort_keys=True), encoding="utf-8")
    summary["baseline_id"] = baseline_id
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a bounded Ditto fleet workload and persist JSON Lines telemetry for the write, search, and freshness SLIs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--base-url", default=DITTO_BASE_URL, help="Ditto HTTP base URL")
    parser.add_argument("--vehicles", type=int, default=8, help="number of fleet vehicles to exercise")
    parser.add_argument("--vehicle-offset", type=int, default=0, help="first vehicle number minus one; use 100 for burst traffic to avoid the measured fleet")
    parser.add_argument("--duration-seconds", type=float, default=120.0, help="how long to run the workload")
    parser.add_argument("--rate-per-second", type=float, default=2.0, help="average update/search rate per second")
    parser.add_argument("--metrics-dir", type=Path, default=Path("data/metrics"), help="where to write JSON Lines metric files")
    parser.add_argument("--no-container-metrics", action="store_true", help="skip the local Docker resource snapshot")
    parser.add_argument("--freshness-method", choices=("sse", "poll-proxy"), default="sse", help="freshness evidence source")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        summary = run_fleet_workload(
            base_url=args.base_url,
            vehicles=args.vehicles,
            vehicle_offset=args.vehicle_offset,
            duration_seconds=args.duration_seconds,
            rate_per_second=args.rate_per_second,
            metrics_dir=args.metrics_dir,
            capture_containers=not args.no_container_metrics,
            freshness_method=args.freshness_method,
        )
    except ValueError as error:
        print(f"invalid workload option: {error}", file=sys.stderr)
        return 2
    except requests.RequestException as error:
        print(f"workload failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
