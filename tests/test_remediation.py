"""Stretch goal: re-run after a fix. Overlay safety, apply/revert mechanics, and the fix verdict."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from ai.remediation import load_registry, main as remediation_main, render_markdown, verify
from ai.retest import compare_retest_runs
from deploy import remediate
from simulator.fleet_workload import SEARCH_TIMEOUT_S

OVERLAY_DIR = ROOT / "deploy" / "remediations" / "search-fail-fast"
BLOCK = re.compile(r"    # >>> remediation: search-fail-fast.*?    # <<< remediation: search-fail-fast\n\n", re.S)


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.base = (ROOT / "deploy" / "ditto" / "nginx.conf").read_text(encoding="utf-8")
        self.overlay = (OVERLAY_DIR / "nginx.conf").read_text(encoding="utf-8")
        self.block = BLOCK.search(self.overlay).group(0)

    def test_overlay_is_the_base_config_plus_one_marked_block(self):
        self.assertEqual(BLOCK.sub("", self.overlay, count=1), self.base, "overlay drifted from deploy/ditto/nginx.conf")

    def test_block_only_touches_search_and_answers_before_the_client_gives_up(self):
        locations = re.findall(r"location (\S+) \{", self.block)
        self.assertEqual(locations, ["/api/2/search", "@search_fail_fast"])
        self.assertIn("auth_basic_user_file          nginx.htpasswd;", self.block, "search must stay authenticated")
        self.assertIn('x-ditto-pre-authenticated "nginx:${remote_user}"', self.block)
        read_timeout = float(re.search(r"proxy_read_timeout\s+(\d+)ms;", self.block).group(1)) / 1000
        self.assertLess(read_timeout, SEARCH_TIMEOUT_S, "nginx must answer before the probe's own timeout")
        self.assertGreaterEqual(read_timeout, 1.0, "must leave headroom over healthy searches")
        self.assertIn("return 504", self.block)
        self.assertIn("Retry-After", self.block)

    def test_compose_override_only_swaps_the_nginx_config(self):
        override = yaml.safe_load((OVERLAY_DIR / "compose.override.yml").read_text(encoding="utf-8"))
        self.assertEqual(list(override["services"]), ["nginx"])
        nginx = override["services"]["nginx"]
        self.assertEqual(list(nginx), ["volumes"], "no ports, images or commands may change")
        self.assertEqual(nginx["volumes"], ["../remediations/search-fail-fast/nginx.conf:/etc/nginx/nginx.conf:ro"])
        self.assertTrue((ROOT / "deploy" / "ditto" / "../remediations/search-fail-fast/nginx.conf").resolve().is_file())

    def test_registry_markers_exist_in_their_overlays(self):
        for name, entry in load_registry().items():
            self.assertIn(entry.get("kind"), ("client", "server"), name)
            if entry.get("overlay"):
                conf = (ROOT / entry["overlay"]).parent / "nginx.conf"
                self.assertIn(entry["marker"], conf.read_text(encoding="utf-8"), name)


class FakeDocker:
    def __init__(self, nginx_config: str = ""):
        self.calls: list[list[str]] = []
        self.nginx_config = nginx_config

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[-2:] == ["nginx", "-T"]:
            return type("R", (), {"stdout": self.nginx_config})()
        if "up" in argv:
            self.nginx_config = "# remediation: search-fail-fast" if any("search-fail-fast" in a for a in argv) else "base"
        return type("R", (), {"stdout": ""})()


class RemediateToolTests(unittest.TestCase):
    ok = staticmethod(lambda url: {"ok": True, "status": 200})

    def test_apply_recreates_only_nginx_with_the_overlay_and_checks_the_running_config(self):
        docker = FakeDocker()
        result = remediate.apply("search-fail-fast", "http://127.0.0.1:8080", runner=docker, check=self.ok, events=None)
        up = next(call for call in docker.calls if "up" in call)
        self.assertEqual(up[-6:], ["up", "-d", "--no-deps", "--timeout", "2", "nginx"])
        self.assertEqual(up.count("-f"), 2)
        self.assertTrue(up[up.index("-p") + 1] == "ditto")
        self.assertEqual(result["active"], ["search-fail-fast"])

    def test_revert_uses_the_base_compose_only(self):
        docker = FakeDocker("# remediation: search-fail-fast")
        result = remediate.revert("search-fail-fast", "http://127.0.0.1:8080", runner=docker, check=self.ok, events=None)
        up = next(call for call in docker.calls if "up" in call)
        self.assertEqual(up.count("-f"), 1)
        self.assertEqual(result["active"], [])

    def test_apply_fails_loudly_if_the_marker_is_not_live(self):
        class Stuck(FakeDocker):
            def __call__(self, argv, **kwargs):
                self.calls.append(argv)
                return type("R", (), {"stdout": "base"})()
        with self.assertRaises(remediate.RemediationError):
            remediate.apply("search-fail-fast", "http://127.0.0.1:8080", runner=Stuck(), check=self.ok, events=None)

    def test_unknown_or_client_side_fixes_cannot_be_deployed(self):
        for name in ("rm-rf", "search-client-timeout"):
            with self.subTest(name), self.assertRaises(remediate.RemediationError):
                remediate.apply(name, "http://127.0.0.1:8080", runner=FakeDocker(), check=self.ok, events=None)

    def test_overlay_outside_the_repo_is_refused(self):
        registry = {"evil": {"overlay": "../../etc/passwd", "service": "nginx", "marker": "x"}}
        with self.assertRaises(remediate.RemediationError):
            remediate._overlay("evil", registry)

    def test_health_check_refuses_non_loopback_urls(self):
        with self.assertRaises(remediate.RemediationError):
            remediate._search_ok("http://example.com")


T0 = datetime(2026, 10, 11, 9, 0, 0, tzinfo=timezone.utc)


def _iso(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def pause_run(run_id: str, active: list[str] | None, *, write_error=0.0, write_p95=40.0, recovered=True,
              action="pause_container", dry_run=False) -> dict:
    run = {
        "run_id": run_id, "experiment_id": f"{action}-things-search-x", "status": "completed",
        "action_params": {"duration_seconds": 30}, "baseline_write_p95_ms": 20.0,
        "diagnosis": {"action": action, "target_component": "things-search"},
        "recovery": {"target": "things-search"}, "recovery_verified": recovered, "recovery_time_seconds": 20.0,
        "cleanup_state": "complete",
        "timeline": {"injected_at": _iso(0), "rollback_at": _iso(30), "recovered_at": _iso(50)},
        "observations": [{"phase": "fault", "write_error_rate": write_error, "write_p95_ms": write_p95,
                          "search_error_rate": 0.9, "freshness_samples": 5, "freshness_p95_seconds": 0.1}],
    }
    if active is not None:
        run["deployment"] = {"remediations_active": active}
    if dry_run:
        run["dry_run"] = True
    return run


def samples(wait_s: float, failure: dict, count: int = 6) -> list[dict]:
    rows = [{"kind": "write", "success": True, "latency_ms": 20.0, "timestamp": _iso(t)} for t in range(0, 50, 2)]
    rows += [{"kind": "search", "success": False, "latency_ms": wait_s * 1000, "timestamp": _iso(5 + i * 4), **failure}
             for i in range(count)]
    return rows


TIMEOUTS = samples(3.0, {"error": "ReadTimeout", "status_code": None})
FAST_504 = samples(1.5, {"status_code": 504})


class VerifierTests(unittest.TestCase):
    def test_client_fix_is_verified_on_the_committed_5_7_evidence(self):
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = remediation_main(["verify", "search-client-timeout", "--evidence"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("before_seconds=10.0, after_seconds=3.0", out.getvalue())

    def test_server_fix_verified_when_failures_become_fast_504s(self):
        result = verify("search-fail-fast", pause_run("before", []), pause_run("after", ["search-fail-fast"]), TIMEOUTS, FAST_504)
        self.assertEqual(result["verdict"], "verified", render_markdown(result))
        self.assertEqual(result["retest"]["overall_improvement"], "server-side fail-fast demonstrated")
        self.assertIn("VERIFIED", render_markdown(result))

    def test_still_timing_out_is_not_verified(self):
        result = verify("search-fail-fast", pause_run("before", []), pause_run("after", ["search-fail-fast"]), TIMEOUTS, TIMEOUTS)
        self.assertEqual(result["verdict"], "not_verified")

    def test_fix_must_be_off_before_and_on_after(self):
        for before, after in (([], []), (["search-fail-fast"], ["search-fail-fast"])):
            with self.subTest(before=before, after=after):
                result = verify("search-fail-fast", pause_run("b", before), pause_run("a", after), TIMEOUTS, FAST_504)
                self.assertEqual(result["verdict"], "not_verified")
        unknown = verify("search-fail-fast", pause_run("b", None), pause_run("a", ["search-fail-fast"]), TIMEOUTS, FAST_504)
        self.assertEqual(unknown["verdict"], "inconclusive")

    def test_guards_catch_regressions(self):
        cases = {
            "write errors": pause_run("a", ["search-fail-fast"], write_error=0.10),
            "no recovery": pause_run("a", ["search-fail-fast"], recovered=False),
        }
        for name, after in cases.items():
            with self.subTest(name):
                self.assertEqual(verify("search-fail-fast", pause_run("b", []), after, TIMEOUTS, FAST_504)["verdict"], "not_verified")

    def test_write_guard_uses_raw_fault_samples_not_leaky_windows(self):
        # Kill-switch windows say 200 ms (a slow write before the fault), raw writes during the fault say 20 ms.
        def with_fault_writes(rows, latency):
            return rows + [{"kind": "write", "success": True, "latency_ms": latency, "timestamp": _iso(1 + t)} for t in range(0, 28, 2)]
        after = pause_run("a", ["search-fail-fast"], write_p95=200.0)
        ok = verify("search-fail-fast", pause_run("b", []), after, with_fault_writes(TIMEOUTS, 20.0), with_fault_writes(FAST_504, 20.0))
        guard = next(item for item in ok["guards"] if item["check"] == "write latency not worse")
        self.assertEqual(guard["status"], "pass")
        self.assertIn("raw samples in fault window", guard["after_source"])
        slow = verify("search-fail-fast", pause_run("b", []), after, with_fault_writes(TIMEOUTS, 20.0), with_fault_writes(FAST_504, 90.0))
        self.assertEqual(slow["verdict"], "not_verified")

    def test_unfair_or_fake_pairs_are_rejected(self):
        other = verify("search-fail-fast", pause_run("b", []), pause_run("a", ["search-fail-fast"], action="restart_container"), TIMEOUTS, FAST_504)
        self.assertEqual(other["verdict"], "not_verified")
        dry = verify("search-fail-fast", pause_run("b", []), pause_run("a", ["search-fail-fast"], dry_run=True), TIMEOUTS, FAST_504)
        self.assertEqual(dry["verdict"], "not_verified")

    def test_missing_evidence_is_inconclusive_never_a_pass(self):
        quiet = [row for row in FAST_504 if row["kind"] == "write"]
        self.assertEqual(verify("search-fail-fast", pause_run("b", []), pause_run("a", ["search-fail-fast"]), TIMEOUTS, quiet)["verdict"], "inconclusive")
        self.assertEqual(verify("search-fail-fast", pause_run("b", []), pause_run("a", ["search-fail-fast"]), TIMEOUTS, [])["verdict"], "inconclusive")

    def test_samples_after_recovery_are_not_counted(self):
        # A fix deploy right after the run drops connections; those failures are not the fault's.
        deploy_blip = [{"kind": "search", "success": False, "latency_ms": 5.0, "timestamp": _iso(52 + i), "error": "ConnectionError"} for i in range(3)]
        result = verify("search-fail-fast", pause_run("b", []), pause_run("a", ["search-fail-fast"]), TIMEOUTS + deploy_blip, FAST_504 + deploy_blip)
        self.assertEqual(result["verdict"], "verified", render_markdown(result))
        self.assertEqual(result["samples_after_recovery_excluded"], {"before": 3, "after": 3})
        self.assertNotIn("ConnectionError", result["request_facts"]["after"]["search_failure_errors"])

    def test_deploys_are_logged(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "events.jsonl"
            remediate.apply("search-fail-fast", "http://127.0.0.1:8080", runner=FakeDocker(), check=lambda url: {"ok": True}, events=log)
            event = __import__("json").loads(log.read_text().splitlines()[0])
        self.assertEqual((event["event"], event["remediation"]), ("apply", "search-fail-fast"))
        self.assertLessEqual(event["started_at"], event["finished_at"])

    def test_unknown_fix_is_refused(self):
        with self.assertRaises(ValueError):
            verify("nope", pause_run("b", []), pause_run("a", []), [], [])


class RetestServerSideTests(unittest.TestCase):
    def test_retest_reports_server_side_fail_fast_only_for_explicit_5xx(self):
        def enriched(run, wait, errors, rate):
            run = copy.deepcopy(run)
            run["_reported_results"] = {"search_failure_wait_seconds": wait, "search_failure_errors": errors, "write_samples_per_second": rate}
            return run
        before = enriched(pause_run("b", []), 3.0, ["ReadTimeout"], 1.0)
        self.assertEqual(compare_retest_runs(before, enriched(pause_run("a", []), 1.5, ["504"], 1.0))["overall_improvement"],
                         "server-side fail-fast demonstrated")
        self.assertNotEqual(compare_retest_runs(before, enriched(pause_run("a", []), 1.5, ["504", "ReadTimeout"], 1.0))["overall_improvement"],
                            "server-side fail-fast demonstrated")


class DeploymentStampTests(unittest.TestCase):
    def test_unreadable_state_is_recorded_as_unknown_not_none(self):
        from unittest import mock
        from safety import cli
        with mock.patch("deploy.remediate.active_remediations", side_effect=RuntimeError("docker down")):
            state = cli.deployment_state()
        self.assertIsNone(state["remediations_active"])
        self.assertEqual(state["error"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
