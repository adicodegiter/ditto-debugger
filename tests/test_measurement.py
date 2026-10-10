from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import requests
from unittest.mock import patch

from ai.report import generate_findings
from monitor.sli import append_jsonl, baseline_summary, load_thresholds, summarize_freshness_samples, summarize_metrics
from monitor.window import baseline_from_metrics, window_stats
from monitor.container import capture_container_metrics
from monitor.sse import FreshnessSseSubscriber, parse_sse_events
from simulator.fleet_workload import _run_freshness_probe, _run_search_probe, _run_write_probe


class MeasurementSliTests(unittest.TestCase):
    def test_vehicle_offset_selects_isolated_burst_twins(self):
        from simulator import fleet_workload
        captured_urls = []

        class FakeSession:
            urls = captured_urls

            def __init__(self):
                self.auth = None

            def put(self, url, **kwargs):
                self.urls.append(url)
                return Mock(status_code=204, json=lambda: {}, text="")

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory, patch.object(fleet_workload.requests, "Session", FakeSession):
            fleet_workload.run_fleet_workload(
                base_url="http://local", vehicles=16, vehicle_offset=100, duration_seconds=0,
                metrics_dir=directory, capture_containers=False, freshness_method="poll-proxy",
            )
        vehicle_urls = [url for url in captured_urls if "/things/bosch.fleet:vehicle-" in url]
        self.assertEqual(
            vehicle_urls,
            [f"http://local/api/2/things/bosch.fleet:vehicle-{index:02d}" for index in range(101, 117)],
        )
        self.assertFalse(any("vehicle-0" in url for url in vehicle_urls))

    def test_vehicle_offset_defaults_to_normal_fleet(self):
        from simulator import fleet_workload
        captured_urls = []

        class FakeSession:
            urls = captured_urls

            def __init__(self):
                self.auth = None

            def put(self, url, **kwargs):
                self.urls.append(url)
                return Mock(status_code=204, json=lambda: {}, text="")

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory, patch.object(fleet_workload.requests, "Session", FakeSession):
            fleet_workload.run_fleet_workload(
                base_url="http://local", vehicles=8, duration_seconds=0,
                metrics_dir=directory, capture_containers=False, freshness_method="poll-proxy",
            )
        vehicle_urls = [url for url in captured_urls if "/things/bosch.fleet:vehicle-" in url]
        self.assertEqual(vehicle_urls[0].rsplit("-", 1)[-1], "01")
        self.assertEqual(vehicle_urls[-1].rsplit("-", 1)[-1], "08")

    def test_vehicle_offset_rejects_invalid_ranges(self):
        from simulator import fleet_workload
        with self.assertRaises(ValueError):
            fleet_workload.run_fleet_workload(vehicle_offset=-1)
        with self.assertRaises(ValueError):
            fleet_workload.run_fleet_workload(vehicles=16, vehicle_offset=990)

    def test_phase_window_outside_samples_is_unhealthy(self):
        from monitor.cli import _parse_timestamp

        with tempfile.TemporaryDirectory() as directory:
            timestamp = "2026-10-10T13:00:00Z"
            for kind in ("write", "search", "freshness"):
                record = {
                    "kind": kind,
                    "source": "live",
                    "timestamp": timestamp,
                    "success": True,
                    "latency_ms": 1,
                }
                if kind == "freshness":
                    record["freshness_seconds"] = 1
                append_jsonl(Path(directory) / f"{kind}.jsonl", record)
            summary = baseline_summary(
                directory,
                window_start=_parse_timestamp("2026-10-10T14:00:00Z"),
                window_end=_parse_timestamp("2026-10-10T14:00:30Z"),
            )
        self.assertEqual(summary["status"], "unhealthy")
        self.assertTrue(any("Missing" in warning for warning in summary["warnings"]))

    def test_sse_parser_reads_json_data_events(self):
        events = list(parse_sse_events([
            "event: thing\n",
            'data: {"thingId":"bosch.fleet:vehicle-01","features":{"telemetry":{"properties":{"last_seen":"2026-10-10T10:00:00Z"}}}}\n',
            "\n",
        ]))
        self.assertEqual(events[0]["thingId"], "bosch.fleet:vehicle-01")

    def test_sse_subscriber_calculates_freshness_from_latest_event(self):
        subscriber = FreshnessSseSubscriber("http://local/api/2", ["bosch.fleet:vehicle-01"], ("ditto", "ditto"))
        subscriber._latest["bosch.fleet:vehicle-01"] = datetime.now(timezone.utc).isoformat()
        subscriber._latest_freshness["bosch.fleet:vehicle-01"] = 0.012
        sample = subscriber.sample("bosch.fleet:vehicle-01")
        self.assertTrue(sample["success"])
        self.assertEqual(sample["freshness_seconds"], 0.012)

    def test_saved_baseline_file_alone_never_becomes_the_baseline(self):
        # A saved file is written when a run *ends*; while a new run is going it would describe
        # the previous run. Baselines come only from the live samples of the latest run.
        with tempfile.TemporaryDirectory() as directory:
            metrics = Path(directory) / "metrics"
            metrics.mkdir()
            baselines = Path(directory) / "baselines"
            baselines.mkdir()
            summary = {"status": "healthy", "write": {}, "search": {}, "freshness": {}}
            (baselines / "baseline-run.json").write_text(json.dumps({
                "baseline_id": "baseline-run",
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "source": "client-sli-jsonl",
                "status": "healthy",
                "summary": summary,
            }), encoding="utf-8")
            baseline = baseline_from_metrics(metrics)
        self.assertEqual(baseline["status"], "unavailable")
        self.assertTrue(baseline["warnings"])

    def test_container_metrics_capture_is_local_and_jsonl(self):
        completed = Mock(returncode=0, stdout='{"Name":"ditto-things-search-1","CPUPerc":"2.5%","MemPerc":"1%"}\n', stderr="")
        with tempfile.TemporaryDirectory() as directory, patch("monitor.container.subprocess.run", return_value=completed) as run:
            records = capture_container_metrics(directory)
            self.assertEqual(records[0]["container"], "ditto-things-search-1")
            self.assertEqual(run.call_args.args[0][:3], ["docker", "stats", "--no-stream"])
            self.assertTrue((Path(directory) / "container.jsonl").is_file())
    def test_window_stats_counts_search_outage_as_error(self):
        with tempfile.TemporaryDirectory() as directory:
            now = datetime(2026, 10, 10, 13, 0, 30, tzinfo=timezone.utc)
            timestamp = "2026-10-10T13:00:29.000Z"
            for kind, success in (("write", True), ("search", False), ("freshness", True)):
                record = {
                    "kind": kind,
                    "source": "live",
                    "timestamp": timestamp,
                    "success": success,
                    "latency_ms": 20,
                }
                if kind == "freshness":
                    record["freshness_seconds"] = 1
                append_jsonl(Path(directory) / f"{kind}.jsonl", record)
            stats = window_stats(directory, last_s=30, now=now)
            self.assertEqual(stats["error_rate"], 0.0)
            self.assertEqual(stats["search_error_rate"], 1.0)
            self.assertTrue(stats["telemetry_available"])
            self.assertTrue(stats["baseline_healthy"])

    def test_report_is_deterministic_and_evidence_linked(self):
        run = {
            "run_id": "run-test",
            "observations": [{"error_rate": 0.5, "telemetry_available": True}],
            "abort_reason": "error_rate_above_configured_limit",
            "recovery_verified": False,
            "recovery": {"target": "things-search"},
        }
        findings = generate_findings(run)
        self.assertEqual(findings[0]["run_id"], "run-test")
        self.assertTrue(findings[0]["evidence"])
        self.assertIn("critical", {finding["severity"] for finding in findings})

    def test_measurement_summary_accepts_write_search_and_freshness_samples(self):
        records = [
            {"kind": "write", "success": True, "latency_ms": 120.0},
            {"kind": "write", "success": True, "latency_ms": 180.0},
            {"kind": "write", "success": False, "latency_ms": 400.0},
            {"kind": "search", "success": True, "latency_ms": 240.0},
            {"kind": "search", "success": True, "latency_ms": 260.0},
            {"kind": "freshness", "observed_at": "2026-10-10T13:00:00Z"},
            {"kind": "freshness", "observed_at": "2026-10-10T13:00:10Z"},
        ]
        summary = summarize_metrics(records)
        self.assertEqual(summary["status"], "unhealthy")
        self.assertEqual(summary["write"]["status"], "degraded")
        self.assertEqual(summary["search"]["status"], "healthy")
        self.assertIn("freshness", summary["freshness"]["kind"])

    def test_freshness_summary_uses_seconds_and_marks_stale_window(self):
        summary = summarize_freshness_samples([
            {"observed_at": "2026-10-10T13:00:00Z", "freshness_seconds": 3.0},
            {"observed_at": "2026-10-10T13:00:20Z", "freshness_seconds": 8.0},
        ])
        self.assertIn("p95_seconds", summary)
        self.assertEqual(summary["p95_seconds"], 8.0)
        self.assertEqual(summary["status"], "healthy")

    def test_freshness_with_only_failed_samples_is_unknown(self):
        summary = summarize_freshness_samples([{"kind": "freshness", "success": False}])
        self.assertEqual(summary["status"], "unknown")

    def test_write_error_threshold_allows_exactly_twenty_percent_but_not_more(self):
        at_threshold = [{"kind": "write", "success": index != 0, "latency_ms": 10} for index in range(5)]
        above_threshold = [{"kind": "write", "success": index > 1, "latency_ms": 10} for index in range(5)]

        self.assertEqual(summarize_metrics(at_threshold)["write"]["status"], "healthy")
        self.assertEqual(summarize_metrics(above_threshold)["write"]["status"], "degraded")
        self.assertEqual(summarize_metrics(above_threshold)["status"], "unhealthy")

    def test_search_misses_are_reported_without_being_http_errors(self):
        summary = summarize_metrics([
            {"kind": "write", "success": True, "latency_ms": 10},
            {"kind": "search", "success": True, "expected_hit": False, "latency_ms": 10},
            {"kind": "search", "success": True, "expected_hit": True, "latency_ms": 10},
            {"kind": "freshness", "success": True, "freshness_seconds": 1},
        ])

        self.assertEqual(summary["search"]["success_rate"], 1.0)
        self.assertEqual(summary["search"]["expected_hit_misses"], 1)
        self.assertEqual(summary["search"]["expected_hit_rate"], 0.5)

    def test_shared_threshold_config_is_loaded(self):
        thresholds = load_thresholds(Path(__file__).resolve().parents[1] / "configs" / "telemetry.yaml")
        self.assertEqual(thresholds["write_error_rate_max"], 0.20)
        self.assertEqual(thresholds["write_p95_multiplier"], 3.0)
        self.assertEqual(thresholds["freshness_p95_seconds_max"], 30.0)

    def test_freshness_summary_labels_poll_proxy(self):
        summary = summarize_freshness_samples([{"freshness_seconds": 1.0}])
        self.assertEqual(summary["method"], "poll-proxy")

    def test_freshness_summary_labels_sse_and_mixed_methods(self):
        sse_summary = summarize_freshness_samples([
            {"freshness_seconds": 1.0, "method": "sse"},
        ])
        mixed_summary = summarize_freshness_samples([
            {"freshness_seconds": 1.0, "method": "sse"},
            {"freshness_seconds": 2.0, "method": "poll-proxy"},
        ])
        self.assertEqual(sse_summary["method"], "sse")
        self.assertEqual(mixed_summary["method"], "mixed")

    def test_jsonl_baseline_summary_handles_missing_kinds(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "write.jsonl").write_text(
                '{"kind":"write","source":"live","run_id":"run-a","success":true,"latency_ms":100,"timestamp":"2026-10-10T13:00:00.000Z"}\n'
                '{"kind":"write","source":"fixture","run_id":"run-a","success":true,"latency_ms":100,"timestamp":"2026-10-10T13:00:01.000Z"}\n',
                encoding="utf-8",
            )
            summary = baseline_summary(base, run_id="run-a", window_start=datetime(2026, 10, 10, 13, 0, tzinfo=timezone.utc))
        self.assertEqual(summary["status"], "unhealthy")
        self.assertTrue(any("Missing" in warning for warning in summary["warnings"]))
        self.assertEqual(summary["write"]["samples"], 1)

    def test_baseline_filters_old_runs_and_fixture_records(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            live = '{"kind":"write","source":"live","run_id":"new","success":true,"latency_ms":10,"timestamp":"2026-10-10T13:00:01.000Z"}\n'
            old = '{"kind":"write","source":"live","run_id":"old","success":false,"latency_ms":10,"timestamp":"2026-10-10T12:00:00.000Z"}\n'
            fixture = '{"kind":"write","source":"fixture","run_id":"new","success":true,"latency_ms":10,"timestamp":"2026-10-10T13:00:02.000Z"}\n'
            (base / "write.jsonl").write_text(live + old + fixture, encoding="utf-8")
            summary = baseline_summary(
                base,
                run_id="new",
                window_start=datetime(2026, 10, 10, 13, tzinfo=timezone.utc),
                window_end=datetime(2026, 10, 10, 13, 0, 2, tzinfo=timezone.utc),
            )
        self.assertEqual(summary["write"]["samples"], 1)

    def test_probes_record_transport_failures_instead_of_raising(self):
        session = Mock()
        session.put.side_effect = requests.ConnectionError("gateway down")
        session.get.side_effect = requests.Timeout("search timeout")

        write = _run_write_probe(session, "bosch.fleet:vehicle-01", value=10, api_base="http://local/api/2", run_id="run-fail")
        search = _run_search_probe(session, "bosch.fleet:vehicle-01", api_base="http://local/api/2", run_id="run-fail")

        self.assertFalse(write["success"])
        self.assertIsNone(write["status_code"])
        self.assertEqual(write["error"], "ConnectionError")
        self.assertFalse(search["success"])
        self.assertIsNone(search["status_code"])
        self.assertEqual(search["error"], "Timeout")

    def test_search_probe_uses_short_timeout(self):
        session = Mock()
        session.get.side_effect = requests.Timeout("search timeout")
        _run_search_probe(session, "bosch.fleet:vehicle-01", api_base="http://local/api/2", run_id="run-timeout")
        self.assertEqual(session.get.call_args.kwargs["timeout"], 3)

    def test_failed_freshness_probe_records_failure_sample(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError("gateway down")

        freshness = _run_freshness_probe(
            session,
            "bosch.fleet:vehicle-01",
            api_base="http://local/api/2",
            run_id="run-fail",
        )

        self.assertFalse(freshness["success"])
        self.assertIsNone(freshness["status_code"])
        self.assertEqual(freshness["error"], "ConnectionError")

    def test_write_probe_uses_feature_level_telemetry_update(self):
        session = Mock()
        response = Mock(status_code=204)
        session.put.return_value = response

        record = _run_write_probe(
            session,
            "bosch.fleet:vehicle-01",
            value=42,
            api_base="http://local/api/2",
            run_id="run-write",
        )

        request = session.put.call_args
        self.assertEqual(request.args[0], "http://local/api/2/things/bosch.fleet:vehicle-01/features/telemetry/properties")
        self.assertEqual(request.kwargs["json"]["speed_kph"], 42)
        self.assertTrue(record["success"])
        self.assertEqual(record["source"], "live")
        self.assertRegex(record["timestamp"], r"\.\d{3}Z$")


if __name__ == "__main__":
    unittest.main()


class M2BaselineAndFreshnessTests(unittest.TestCase):
    """Review fixes for PR #8: arrival-time freshness, concurrent probes, run-scoped 120 s baselines."""

    def _write_run(self, base, run_id, start, seconds, step=10.0, success=True):
        from datetime import timedelta
        t = 0.0
        while t <= seconds:
            ts = (start + timedelta(seconds=t)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            common = {"source": "live", "run_id": run_id, "timestamp": ts}
            append_jsonl(base / "write.jsonl", {"kind": "write", "success": success, "latency_ms": 20.0, **common})
            append_jsonl(base / "search.jsonl", {"kind": "search", "success": True, "latency_ms": 25.0, **common})
            append_jsonl(base / "freshness.jsonl", {"kind": "freshness", "success": True, "method": "sse", "freshness_seconds": 0.05, **common})
            t += step

    def test_sse_freshness_is_measured_when_the_event_arrives(self):
        from datetime import timedelta
        from monitor.sse import arrival_record
        written = datetime(2026, 10, 10, 13, 0, 0, tzinfo=timezone.utc)
        record = arrival_record("bosch.fleet:vehicle-01", "2026-10-10T13:00:00Z", received_at=written + timedelta(milliseconds=80))
        self.assertTrue(record["success"])
        self.assertAlmostEqual(record["freshness_seconds"], 0.08, places=3)
        self.assertFalse(arrival_record("v", "not-a-time")["success"])

    def test_failed_freshness_reads_do_not_inflate_the_percentile(self):
        summary = summarize_freshness_samples([
            {"success": True, "freshness_seconds": 1.0},
            {"success": False, "observed_at": "2026-10-10T10:00:00Z", "error": "ReadTimeout"},
        ], now=datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc))
        self.assertEqual(summary["p95_seconds"], 1.0)

    def test_baseline_uses_latest_run_only_and_needs_120_seconds(self):
        from datetime import timedelta
        from monitor.window import baseline_from_metrics
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self._write_run(base, "run-old", now - timedelta(hours=2), 300, success=False)  # unhealthy, old
            self._write_run(base, "run-new", now - timedelta(seconds=90), 55)               # healthy but too short (ends now-35 s)
            short = baseline_from_metrics(base)
            self._write_run(base, "run-long", now - timedelta(seconds=150), 145)            # healthy, 145 s
            long = baseline_from_metrics(base)
        self.assertEqual(short["status"], "unavailable")
        self.assertEqual(short["run_id"], "run-new")
        self.assertTrue(any("120" in warning for warning in short["warnings"]))
        self.assertEqual(long["status"], "available", long["warnings"])
        self.assertEqual(long["run_id"], "run-long")
        self.assertEqual(long["telemetry"]["write"]["error_rate"], 0.0, "the failing old run must not leak in")

    def test_capture_baseline_works_with_saved_baseline_files_present(self):
        import sys
        from datetime import timedelta
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
        from app.store import LocalRepository
        from monitor.window import baseline_from_metrics
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metrics = root / "metrics"
            metrics.mkdir()
            (root / "baselines").mkdir()
            (root / "baselines" / "baseline-run-x.json").write_text('{"status": "healthy", "summary": {}}', encoding="utf-8")
            self._write_run(metrics, "run-live", now - timedelta(seconds=130), 125)
            stored = LocalRepository(root / "db.sqlite3").save_baseline("graph-1", baseline_from_metrics(metrics))
        self.assertEqual(stored["telemetry_status"], "available")

    def test_hung_search_does_not_stall_writes(self):
        import time as real_time
        from simulator import fleet_workload

        class FakeSession:
            def __init__(self):
                self.auth = None

            def put(self, url, **kwargs):
                return Mock(status_code=204, json=lambda: {}, text="")

            def get(self, url, **kwargs):
                if "/search/" in url:
                    real_time.sleep(1.0)  # a hung search, shorter than the 3 s timeout
                return Mock(status_code=200, json=lambda: {"items": [1]} if "/search/" in url else "2026-10-10T13:00:00Z", text="")

        with tempfile.TemporaryDirectory() as directory, patch.object(fleet_workload.requests, "Session", FakeSession):
            fleet_workload.run_fleet_workload(
                base_url="http://local", vehicles=2, duration_seconds=2.5, rate_per_second=20.0,
                metrics_dir=directory, capture_containers=False, freshness_method="poll-proxy",
            )
            writes = len(Path(directory, "write.jsonl").read_text(encoding="utf-8").splitlines())
            searches = len(Path(directory, "search.jsonl").read_text(encoding="utf-8").splitlines())
        self.assertGreaterEqual(writes, 4 * searches, f"writes={writes} searches={searches}: writes must not wait for search")
        self.assertEqual(fleet_workload.SEARCH_TIMEOUT_S, 3.0)


class SseDeliveryTests(unittest.TestCase):
    def test_subscriber_reads_events_unbuffered_and_reports_arrival_staleness(self):
        events = []
        line = 'data: {"thingId":"bosch.fleet:vehicle-01","features":{"telemetry":{"properties":{"last_seen":"%s"}}}}'
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        calls = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def raise_for_status(self):
                return None

            def iter_lines(self, chunk_size=512, decode_unicode=False):
                calls["chunk_size"] = chunk_size
                subscriber._stop.set()
                return iter([line % now, ""])

        subscriber = FreshnessSseSubscriber("http://local/api/2", ["bosch.fleet:vehicle-01"], ("ditto", "ditto"), on_event=events.append)
        with patch("monitor.sse.requests.get", return_value=Response()):
            subscriber._consume()
        self.assertEqual(calls["chunk_size"], 1, "SSE must not be read in buffered chunks")
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["success"])
        self.assertLess(events[0]["freshness_seconds"], 5)
