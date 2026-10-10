from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

import requests


def parse_sse_events(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    """Yield JSON SSE data payloads from a text/event-stream response."""
    data_lines: list[str] = []
    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        line = line.rstrip("\r\n")
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                try:
                    value = json.loads(payload)
                except json.JSONDecodeError:
                    value = None
                if isinstance(value, dict):
                    yield value
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        try:
            value = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            yield value


def _find_last_seen(value: Any) -> str | None:
    if isinstance(value, dict):
        candidate = value.get("last_seen")
        if isinstance(candidate, str):
            return candidate
        for child in value.values():
            found = _find_last_seen(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_last_seen(child)
            if found:
                return found
    return None


def _find_thing_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("thingId", "thing_id"):
            if isinstance(value.get(key), str):
                return value[key]
        for child in value.values():
            found = _find_thing_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_thing_id(child)
            if found:
                return found
    return None


class FreshnessSseSubscriber:
    """Local Ditto SSE subscriber that tracks the latest telemetry timestamp."""

    def __init__(self, api_base: str, vehicle_ids: list[str], auth: tuple[str, str]) -> None:
        self.url = f"{api_base.rstrip('/')}/things"
        self.vehicle_ids = set(vehicle_ids)
        self.auth = auth
        self._latest: dict[str, str] = {}
        self._latest_freshness: dict[str, float] = {}
        self._error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._consume, name="ditto-freshness-sse", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def wait_until_ready(self, timeout: float = 10.0) -> bool:
        deadline = datetime.now(timezone.utc).timestamp() + timeout
        while datetime.now(timezone.utc).timestamp() < deadline:
            if self.vehicle_ids.issubset(self._latest):
                return True
            if self._stop.wait(0.1):
                return False
        return self.vehicle_ids.issubset(self._latest)

    def sample(self, vehicle_id: str) -> dict[str, Any]:
        observed_at = self._latest.get(vehicle_id)
        if observed_at is None:
            return {"success": False, "error": self._error or "SSENoTelemetryEvent"}
        freshness = self._latest_freshness.get(vehicle_id)
        if freshness is not None:
            return {
                "success": True,
                "observed_at": observed_at,
                "freshness_seconds": freshness,
            }
        try:
            parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            freshness = max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
        except ValueError:
            return {"success": False, "error": "InvalidTelemetryTimestamp", "observed_at": observed_at}
        return {
            "success": True,
            "observed_at": observed_at,
            "freshness_seconds": freshness,
        }

    def _consume(self) -> None:
        while not self._stop.is_set():
            try:
                with requests.get(
                    self.url,
                    params={
                        "ids": ",".join(sorted(self.vehicle_ids)),
                        "fields": "thingId,features/telemetry/properties/last_seen",
                        "extraFields": "features/telemetry/properties/last_seen",
                    },
                    headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
                    auth=self.auth,
                    stream=True,
                    timeout=(10, 30),
                ) as response:
                    response.raise_for_status()
                    self._error = None
                    for event in parse_sse_events(response.iter_lines(decode_unicode=True)):
                        vehicle_id = _find_thing_id(event)
                        last_seen = _find_last_seen(event)
                        if vehicle_id in self.vehicle_ids and last_seen:
                            received_at = datetime.now(timezone.utc)
                            try:
                                telemetry_at = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                                if telemetry_at.tzinfo is None:
                                    telemetry_at = telemetry_at.replace(tzinfo=timezone.utc)
                                freshness = max(
                                    0.0,
                                    (received_at - telemetry_at.astimezone(timezone.utc)).total_seconds(),
                                )
                            except ValueError:
                                continue
                            self._latest[vehicle_id] = last_seen
                            self._latest_freshness[vehicle_id] = freshness
                        if self._stop.is_set():
                            return
            except (requests.RequestException, OSError) as error:
                self._error = type(error).__name__
                self._stop.wait(1)
