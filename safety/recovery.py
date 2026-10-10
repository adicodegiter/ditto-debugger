"""Functional recovery check against the local Ditto API (via nginx)."""
from __future__ import annotations

import base64
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

# Read-only canaries that exercise the path the target serves.
CANARY_PATHS = {
    "things-search": "/api/2/search/things?option=size(1)",
}
DEFAULT_CANARY_PATH = "/api/2/search/things?option=size(1)"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class HttpRecoveryChecker:
    """Healthy = nginx-routed /health answers 200 AND an authenticated canary answers 200.

    Ditto's per-service /alive endpoint lives on the internal port 7626 and is not routed
    by nginx, so /health is the externally reachable health endpoint.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout_seconds: float = 5.0,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        host = urlsplit(base_url).hostname or ""
        if host not in LOOPBACK_HOSTS:
            raise ValueError("Recovery checks only contact a loopback Ditto URL.")
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self._auth_header = f"Basic {token}"
        self.timeout_seconds = timeout_seconds
        self._open = opener

    def _status(self, path: str, authenticated: bool) -> int | None:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = self._auth_header
        try:
            with self._open(Request(f"{self.base_url}{path}", headers=headers), timeout=self.timeout_seconds) as response:
                return int(response.status)
        except HTTPError as error:
            return int(error.code)
        except (URLError, OSError, ValueError):
            return None

    def verify(self, target: str) -> dict[str, Any]:
        canary_path = CANARY_PATHS.get(target, DEFAULT_CANARY_PATH)
        health_status = self._status("/health", authenticated=False)
        canary_status = self._status(canary_path, authenticated=True)
        return {
            "target": target,
            "healthy": health_status == 200,
            "canary": canary_status == 200,
            "health_status": health_status,
            "canary_status": canary_status,
            "canary_path": canary_path,
        }
