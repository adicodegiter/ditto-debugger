#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor.sli import append_jsonl, baseline_summary, load_thresholds, utc_timestamp  # noqa: E402
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
            timeout=10,
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
        response = session.get(f"{api_base}/search/things", params={"filter": search}, timeout=10)
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
        response = session.get(f"{api_base}/things/{vehicle_id}/features/telemetry/properties/last_seen", timeout=10)
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


def run_fleet_workload(*, base_url: str = DITTO_BASE_URL, vehicles: int = 8, duration_seconds: float = 30.0, rate_per_second: float = 2.0, metrics_dir: str | Path = "data/metrics") -> dict:
    metrics_path = Path(metrics_dir)
    metrics_path.mkdir(parents=True, exist_ok=True)
    api_base = _resolve_api_base(base_url)
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    session = requests.Session()
    session.auth = AUTH

    policy_response = session.put(f"{api_base}/policies/{POLICY_ID}", json=policy_body(), timeout=10)
    if policy_response.status_code not in {200, 201, 204}:
        raise RuntimeError(f"Unable to create the fleet policy ({policy_response.status_code}): {policy_response.text}")

    deadline = time.monotonic() + float(duration_seconds)
    vehicle_ids = [f"{NAMESPACE}:vehicle-{idx:02d}" for idx in range(1, vehicles + 1)]
    for vehicle_id in vehicle_ids:
        _ensure_vehicle(session, vehicle_id, api_base)

    while time.monotonic() < deadline:
        for vehicle_id in vehicle_ids:
            if time.monotonic() >= deadline:
                break
            value = int((time.monotonic() * 1000) % 100)
            record = _run_write_probe(session, vehicle_id, value=value, api_base=api_base, run_id=run_id)
            append_jsonl(metrics_path / "write.jsonl", record)

            search_record = _run_search_probe(session, vehicle_id, api_base=api_base, run_id=run_id)
            append_jsonl(metrics_path / "search.jsonl", search_record)

            freshness_record = _run_freshness_probe(session, vehicle_id, api_base=api_base, run_id=run_id)
            append_jsonl(metrics_path / "freshness.jsonl", freshness_record)

            time.sleep(max(0.0, 1.0 / max(rate_per_second, 0.1)))

    thresholds_path = Path(__file__).resolve().parents[1] / "configs" / "telemetry.yaml"
    summary = baseline_summary(metrics_path, run_id=run_id, thresholds=load_thresholds(thresholds_path))
    summary["run"] = {
        "run_id": run_id,
        "base_url": base_url,
        "vehicles": vehicles,
        "duration_seconds": duration_seconds,
        "rate_per_second": rate_per_second,
        "captured_at": utc_timestamp(),
    }
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a bounded Ditto fleet workload and persist JSON Lines telemetry for the write, search, and freshness SLIs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--base-url", default=DITTO_BASE_URL, help="Ditto HTTP base URL")
    parser.add_argument("--vehicles", type=int, default=8, help="number of fleet vehicles to exercise")
    parser.add_argument("--duration-seconds", type=float, default=30.0, help="how long to run the workload")
    parser.add_argument("--rate-per-second", type=float, default=2.0, help="average update/search rate per second")
    parser.add_argument("--metrics-dir", type=Path, default=Path("data/metrics"), help="where to write JSON Lines metric files")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        summary = run_fleet_workload(
            base_url=args.base_url,
            vehicles=args.vehicles,
            duration_seconds=args.duration_seconds,
            rate_per_second=args.rate_per_second,
            metrics_dir=args.metrics_dir,
        )
    except requests.RequestException as error:
        print(f"workload failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
