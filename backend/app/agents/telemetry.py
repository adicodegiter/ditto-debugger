from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


SAFE_LABELS = {
    "job", "instance", "service", "component", "measure", "collector", "segment", "ditto_statusCode", "mode", "pool"
}
METRIC_QUERIES = ("roundtrip_http_seconds_count", "jvm_memory_bytes_sum", "jvm_threads")


@dataclass(frozen=True)
class TelemetryObservation:
    metric: str
    value: float
    observed_at: str
    source: str
    labels: dict[str, str]
    quality: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "metric": self.metric,
            "value": self.value,
            "observed_at": self.observed_at,
            "source": self.source,
            "labels": self.labels,
            "quality": self.quality,
        }


def normalize_prometheus_result(result: Any, metric_name: str, now_epoch: float | None = None, stale_after_seconds: int = 30) -> list[TelemetryObservation]:
    if not isinstance(result, list):
        return []
    current_time = now_epoch if now_epoch is not None else time.time()
    output: list[TelemetryObservation] = []
    for row in result:
        if not isinstance(row, dict):
            continue
        metric = row.get("metric", {})
        value = row.get("value")
        if not isinstance(metric, dict) or not isinstance(value, list) or len(value) != 2:
            continue
        try:
            timestamp = float(value[0])
            numeric_value = float(value[1])
        except (TypeError, ValueError):
            continue
        observed_at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        safe_labels = {str(key): str(item)[:120] for key, item in metric.items() if key in SAFE_LABELS}
        output.append(TelemetryObservation(
            metric=metric_name,
            value=numeric_value,
            observed_at=observed_at,
            source="prometheus",
            labels=safe_labels,
            quality="stale" if current_time - timestamp > stale_after_seconds else "fresh",
        ))
    return output


class TelemetryObservabilityAgent:
    name = "Telemetry and Observability Agent"

    def __init__(self, prometheus_url: str, timeout_seconds: float = 2.0, stale_after_seconds: int = 30) -> None:
        self.prometheus_url = prometheus_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.stale_after_seconds = stale_after_seconds

    def collect(self) -> dict[str, Any]:
        if not self._is_loopback(self.prometheus_url):
            return self._unavailable("Prometheus URL must use loopback; remote scrape targets are not contacted by this MVP.")
        observations: list[TelemetryObservation] = []
        metric_families: set[str] = set()
        try:
            for query in ("up", *METRIC_QUERIES):
                payload = self._query(query)
                data = payload.get("data", {})
                if payload.get("status") != "success" or not isinstance(data, dict):
                    return self._unavailable("Prometheus returned an unsuccessful query response.")
                query_observations = normalize_prometheus_result(data.get("result"), query, stale_after_seconds=self.stale_after_seconds)
                if query != "up" and query_observations:
                    metric_families.add(query)
                observations.extend(query_observations)
        except (OSError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
            return self._unavailable(f"Prometheus is unavailable ({type(error).__name__}).")

        target_observations = [item for item in observations if item.metric == "up"]
        if not target_observations:
            return self._unavailable("Prometheus has no scraped targets to assess.")
        fresh_targets = [item for item in target_observations if item.quality == "fresh"]
        healthy_targets = [item for item in fresh_targets if item.value == 1.0]
        target_coverage = len(fresh_targets) / len(target_observations)
        metric_coverage = len(metric_families) / len(METRIC_QUERIES)
        coverage = 0.7 * target_coverage + 0.3 * metric_coverage
        has_request_metric = "roundtrip_http_seconds_count" in metric_families
        has_jvm_metric = any(query.startswith("jvm_") for query in metric_families)
        status = "available" if len(healthy_targets) == len(target_observations) and has_request_metric and has_jvm_metric else "degraded"
        return {
            "schema_version": "1.0",
            "status": status,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "coverage": coverage,
            "target_count": len(target_observations),
            "healthy_target_count": len(healthy_targets),
            "observations": [item.to_dict() for item in observations],
            "warnings": [] if status == "available" else ["Scrape targets or required request/JVM metric families are down, stale, or missing; planning is blocked."],
        }

    def _query(self, query: str) -> dict[str, Any]:
        url = f"{self.prometheus_url}/api/v1/query?{urlencode({'query': query})}"
        request = Request(url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            if not self._is_loopback(response.geturl()):
                raise ValueError("Prometheus redirected to a non-loopback URL.")
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _is_loopback(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}

    @staticmethod
    def _unavailable(reason: str) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "status": "unavailable",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "coverage": 0.0,
            "target_count": 0,
            "healthy_target_count": 0,
            "observations": [],
            "warnings": [reason],
        }