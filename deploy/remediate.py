"""Apply, revert or check a registered fix on the local Ditto stack (stretch goal: re-run after a fix).

Only fixes listed in configs/remediations.yaml with an overlay can be applied, and an overlay may
recreate only the one service it names (with --no-deps). This is a deploy step run by a human,
like deploy/reset.sh; it is not a fault and never runs from the API or an agent.

    python deploy/remediate.py list
    python deploy/remediate.py status
    python deploy/remediate.py apply search-fail-fast
    python deploy/remediate.py revert search-fail-fast

Recreating nginx drops API connections for about STOP_TIMEOUT_SECONDS. Do not apply or revert
while an experiment runs, and take the next baseline only after its window has moved past the gap.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "configs" / "remediations.yaml"
COMPOSE_FILE = REPO_ROOT / "deploy" / "ditto" / "docker-compose.yml"
PINS = REPO_ROOT / "deploy" / "image-pins.env"
PROJECT = "ditto"
EVENTS = REPO_ROOT / "data" / "remediation-events.jsonl"
# Recreating nginx waits for open connections to close; the workload's SSE stream holds one open,
# so the default 10 s stop timeout meant ~10 s with no API (measured 2026-10-11). 2 s is enough
# for in-flight requests and keeps the gap short.
STOP_TIMEOUT_SECONDS = 2
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class RemediationError(RuntimeError):
    pass


def load_registry(path: Path = REGISTRY) -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    entries = raw.get("remediations")
    if not isinstance(entries, dict):
        raise RemediationError("configs/remediations.yaml must have a 'remediations' mapping.")
    return entries


def _overlay(name: str, registry: dict[str, dict[str, Any]]) -> tuple[Path, str, str]:
    entry = registry.get(name)
    if entry is None:
        raise RemediationError(f"Unknown remediation '{name}'. Known: {', '.join(sorted(registry))}.")
    overlay, service, marker = entry.get("overlay"), entry.get("service"), entry.get("marker")
    if not (overlay and service and marker):
        raise RemediationError(f"'{name}' has no deployable overlay (kind: {entry.get('kind')}).")
    path = (REPO_ROOT / overlay).resolve()
    if REPO_ROOT.resolve() not in path.parents or not path.is_file():
        raise RemediationError("The overlay must be a file inside this repository.")
    return path, str(service), str(marker)


def _base(overlay: Path | None = None) -> list[str]:
    command = ["docker", "compose", "-p", PROJECT, "--env-file", str(PINS), "-f", str(COMPOSE_FILE)]
    return command + (["-f", str(overlay)] if overlay else [])


def _run(argv: list[str], runner: Callable[..., Any]) -> str:
    try:
        result = runner(argv, check=True, capture_output=True, text=True, timeout=120)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip().splitlines()[-1:] or [""]
        raise RemediationError(f"{' '.join(argv[:4])} ... failed: {detail[0]}") from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RemediationError(f"could not run docker: {type(error).__name__}") from error
    return result.stdout or ""


def active_remediations(registry: dict[str, dict[str, Any]] | None = None,
                        runner: Callable[..., Any] = subprocess.run) -> list[str]:
    """Fixes whose marker is in the RUNNING nginx config (`nginx -T`), not what a file says."""
    registry = registry if registry is not None else load_registry()
    services: dict[str, str] = {}
    active = []
    for name, entry in sorted(registry.items()):
        service, marker = entry.get("service"), entry.get("marker")
        if not service or not marker:
            continue
        if service not in services:
            services[service] = _run([*_base(), "exec", "-T", service, "nginx", "-T"], runner)
        if marker in services[service]:
            active.append(name)
    return active


def _search_ok(base_url: str, timeout: float = 5.0) -> dict[str, Any]:
    """One authenticated search through nginx: the fix must not break healthy searches."""
    if (urlsplit(base_url).hostname or "") not in LOOPBACK:
        raise RemediationError("Only a loopback Ditto URL is checked.")
    user = os.environ.get("DITTO_USERNAME") or os.environ.get("DITTO_USER") or "ditto"
    password = os.environ.get("DITTO_PASSWORD") or os.environ.get("DITTO_PASS") or "ditto"
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = Request(f"{base_url.rstrip('/')}/api/2/search/things?option=size(1)", headers={"Authorization": f"Basic {token}"})
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(response.status)
    except HTTPError as error:
        status = int(error.code)
    except (URLError, OSError):
        status = None
    return {"status": status, "ok": status == 200, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}


def _settle(check: Callable[[], dict[str, Any]], attempts: int = 10, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for _ in range(attempts):
        result = check()
        if result.get("ok"):
            return result
        sleep(1.0)
    return result


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _log(event: dict[str, Any], path: Path | None) -> None:
    """Deploy windows, so analysis can tell a fix deploy's brief API gap from a fault's effect."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def _recreate(argv_base: list[str], service: str, runner: Callable[..., Any]) -> None:
    _run([*argv_base, "up", "-d", "--no-deps", "--timeout", str(STOP_TIMEOUT_SECONDS), service], runner)


def apply(name: str, base_url: str, runner: Callable[..., Any] = subprocess.run, check=_search_ok,
          events: Path | None = EVENTS) -> dict[str, Any]:
    registry = load_registry()
    overlay, service, _ = _overlay(name, registry)
    started = _now()
    _recreate(_base(overlay), service, runner)
    active = active_remediations(registry, runner)
    search = _settle(lambda: check(base_url))
    _log({"event": "apply", "remediation": name, "service": service, "started_at": started, "finished_at": _now(),
          "active": active, "healthy_search": search.get("ok")}, events)
    if name not in active:
        raise RemediationError(f"Applied, but the running {service} config does not contain the '{name}' marker.")
    return {"remediation": name, "action": "apply", "active": active, "healthy_search": search, "applied_at": started}


def revert(name: str, base_url: str, runner: Callable[..., Any] = subprocess.run, check=_search_ok,
           events: Path | None = EVENTS) -> dict[str, Any]:
    registry = load_registry()
    _, service, _ = _overlay(name, registry)
    started = _now()
    _recreate(_base(), service, runner)
    active = active_remediations(registry, runner)
    search = _settle(lambda: check(base_url))
    _log({"event": "revert", "remediation": name, "service": service, "started_at": started, "finished_at": _now(),
          "active": active, "healthy_search": search.get("ok")}, events)
    if name in active:
        raise RemediationError(f"Reverted, but the running {service} config still contains the '{name}' marker.")
    return {"remediation": name, "action": "revert", "active": active, "healthy_search": search, "reverted_at": started}


def _experiment_running() -> bool:
    """Best effort (pgrep, not on Windows): never recreate nginx in the middle of a fault."""
    if os.name == "nt":
        return False
    try:
        return subprocess.run(["pgrep", "-f", "safety.cli execute"], capture_output=True).returncode == 0
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply, revert or check a registered fix on the local Ditto stack.")
    parser.add_argument("--base-url", default=os.environ.get("DITTO_BASE_URL", "http://127.0.0.1:8080"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show the registered fixes")
    sub.add_parser("status", help="which fixes are active in the RUNNING containers")
    for name in ("apply", "revert"):
        command = sub.add_parser(name, help=f"{name} one registered fix (recreates only its service)")
        command.add_argument("remediation")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            for key, entry in load_registry().items():
                print(f"{key} [{entry.get('kind')}]: {entry.get('change')}")
                print(f"   addresses: {entry.get('addresses')}")
            return 0
        if args.command == "status":
            print(json.dumps({"active": active_remediations()}, indent=2))
            return 0
        if args.command in ("apply", "revert") and _experiment_running():
            raise RemediationError("An experiment is running (safety.cli execute). Apply or revert after it finishes.")
        result = (apply if args.command == "apply" else revert)(args.remediation, args.base_url)
    except RemediationError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["healthy_search"].get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
