#!/usr/bin/env python3
"""End-to-end smoke test of the local Ditto API.

Steps: create policy -> create thing -> update a feature property ->
read it back -> search for the thing. Prints PASS/FAIL per step and exits
non-zero if any step fails. Safe to re-run (all writes are idempotent PUTs).
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from simulator.config import API, AUTH, DITTO_BASE_URL, NAMESPACE, POLICY_ID, policy_body  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # Never a fleet vehicle ID: PUT replaces the whole twin, so reusing vehicle-01 would wipe
    # the workload's telemetry feature and make every fleet write to it return 404.
    ap.add_argument("--thing-id", default=f"{NAMESPACE}:smoke-test", help="thing to create (default: %(default)s)")
    ap.add_argument("--search-timeout", type=float, default=30.0,
                    help="seconds to wait for the search index to catch up (default: %(default)s)")
    args = ap.parse_args()

    s = requests.Session()
    s.auth = AUTH
    thing_url = f"{API}/things/{args.thing_id}"
    level = random.randint(20, 100)
    results: list[tuple[str, bool, str]] = []

    def step(name: str, fn) -> bool:
        t0 = time.perf_counter()
        try:
            ok, detail = fn()
        except requests.RequestException as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        ms = (time.perf_counter() - t0) * 1000
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<28} {ms:7.1f} ms  {detail}")
        return ok

    def create_policy():
        r = s.put(f"{API}/policies/{POLICY_ID}", json=policy_body(), timeout=10)
        return r.status_code in (201, 204), f"PUT policy {POLICY_ID} -> {r.status_code}"

    def create_thing():
        body = {
            "policyId": POLICY_ID,
            "attributes": {"model": "smoke-test", "fleet": "bosch"},
            "features": {"battery": {"properties": {"level": 100}}},
        }
        r = s.put(thing_url, json=body, timeout=10)
        return r.status_code in (201, 204), f"PUT thing {args.thing_id} -> {r.status_code}"

    def update_feature():
        r = s.put(f"{thing_url}/features/battery/properties/level", json=level, timeout=10)
        return r.status_code in (201, 204), f"PUT battery.level={level} -> {r.status_code}"

    def read_back():
        r = s.get(f"{thing_url}/features/battery/properties/level", timeout=10)
        if r.status_code != 200:
            return False, f"GET -> {r.status_code}"
        got = r.json()
        return got == level, f"GET -> {r.status_code}, value={got} (expected {level})"

    def search():
        # Search index is eventually consistent: poll until the updated value is visible.
        flt = f'and(eq(thingId,"{args.thing_id}"),eq(features/battery/properties/level,{level}))'
        deadline = time.monotonic() + args.search_timeout
        attempts = 0
        while True:
            attempts += 1
            r = s.get(f"{API}/search/things", params={"filter": flt}, timeout=10)
            items = r.json().get("items", []) if r.status_code == 200 else []
            if items:
                return True, f"GET search -> {r.status_code}, {len(items)} hit(s) after {attempts} attempt(s)"
            if time.monotonic() > deadline:
                return False, f"GET search -> {r.status_code}, 0 hits after {attempts} attempts"
            time.sleep(0.5)

    print(f"Ditto smoke test against {DITTO_BASE_URL}\n")
    # Each step depends on the previous one, so stop at the first failure.
    for name, fn in [("create policy", create_policy), ("create thing", create_thing),
                     ("update feature property", update_feature), ("read back property", read_back),
                     ("search query", search)]:
        if not step(name, fn):
            break

    passed = sum(ok for _, ok, _ in results)
    total = 5
    print(f"\n{'PASS' if passed == total else 'FAIL'}: {passed}/{total} steps passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
