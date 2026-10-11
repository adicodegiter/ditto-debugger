"""Combined failures: policy rules, composition, staged runner, rollback ordering, interaction effect."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from ai.compound import compose_compound, interaction_effect, interaction_finding, merge_predictions, suggest_pairs
from safety.docker_adapter import DockerCommandError
from safety.observations import unrelated_services_healthy
from safety.policy import Policy, SafetyViolation
from safety.runner import ExperimentRunner
from tests.test_safety import HEALTHY, FakeClock, FakeRecovery, FakeWatchdog, ScriptedObservations

POLICY_PATH = ROOT / "configs" / "experiment-policy.yaml"


def single(action: str, target: str, duration: int, risk: float, slis: dict[str, str]) -> dict:
    return {
        "schema_version": "1.0", "id": f"{action}-{target}-x", "target_component": target, "action": action,
        "duration_seconds": duration, "hypothesis": "h", "predicted_impacts": [target], "predicted_slis": slis,
        "risk_score": risk, "information_gain": 0.5, "approval_required": True,
        "evidence_refs": ["graph:g1", "baseline:b1"],
    }


BURST = single("traffic_burst", "nginx", 45, 0.15, {"write": "degrade", "search": "degrade", "freshness": "hold"})
PAUSE = single("pause_container", "things-search", 30, 0.3, {"write": "hold", "search": "fail", "freshness": "hold"})


class RecordingDocker:
    def __init__(self, fail_rollback_for: str | None = None, fail_preflight: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail_rollback_for = fail_rollback_for
        self.fail_preflight = fail_preflight

    def preflight(self, target):
        self.calls.append(("preflight", "", target))
        if self.fail_preflight and target == "things-search":
            raise DockerCommandError("not running")

    def capture(self, action, target):
        return {}

    def apply(self, action, target, params=None, state=None):
        self.calls.append(("apply", action, target))

    def rollback(self, action, target, state=None):
        self.calls.append(("rollback", action, target))
        if target == self.fail_rollback_for:
            raise DockerCommandError("boom")

    def order(self, kind):
        return [action for what, action, _ in self.calls if what == kind]


class RecordingObservations(ScriptedObservations):
    def __init__(self, *values):
        super().__init__(*values)
        self.targets: list[object] = []

    def observe(self, target, since=None):
        self.targets.append(target)
        return super().observe(target, since)


class CompoundPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = Policy.from_file(POLICY_PATH)
        self.good = compose_compound(BURST, PAUSE, 10)

    def test_valid_compound_passes(self):
        self.policy.validate_candidate(self.good)
        self.assertEqual(self.good["duration_seconds"], 45)
        self.assertAlmostEqual(self.good["risk_score"], 0.405, places=3)

    def test_rejections(self):
        def mutated(**changes):
            return {**self.good, **changes}
        faults = self.good["faults"]
        cases = {
            "single fault": mutated(faults=faults[:1]),
            "three faults": mutated(faults=faults + [dict(faults[0], target_component="gateway")]),
            "same target twice": mutated(faults=[faults[0], dict(faults[1], target_component="nginx")]),
            "offset over stagger limit": mutated(faults=[faults[0], dict(faults[1], start_offset_seconds=21)], duration_seconds=51),
            "wrong total duration": mutated(duration_seconds=44),
            "over max duration": mutated(faults=[faults[0], dict(faults[1], start_offset_seconds=20, duration_seconds=45)], duration_seconds=65),
            "risk understated": mutated(risk_score=0.1),
            "no approval flag": mutated(approval_required=False),
            "no hypothesis": mutated(hypothesis=""),
            "unknown action": mutated(faults=[faults[0], dict(faults[1], action="run_shell")]),
            "bool offset": mutated(faults=[faults[0], dict(faults[1], start_offset_seconds=True)]),
        }
        for name, item in cases.items():
            with self.subTest(name), self.assertRaises(SafetyViolation):
                self.policy.validate_candidate(item)

    def test_combined_run_ends_one_kill_switch_poll_before_the_cap(self):
        faults = self.good["faults"]
        limit = self.policy.max_duration_seconds - self.policy.thresholds.poll_interval_seconds

        def ending_at(end):
            second = dict(faults[1], start_offset_seconds=15, duration_seconds=end - 15)
            return {**self.good, "faults": [faults[0], second], "duration_seconds": end}

        self.policy.validate_candidate(ending_at(int(limit)))
        # Ending exactly at the cap always aborted on the deadline check (#18), so it is refused up front.
        with self.assertRaises(SafetyViolation):
            self.policy.validate_candidate(ending_at(self.policy.max_duration_seconds))

    def test_mongodb_and_unlisted_pairs_are_never_combinable(self):
        mongo = single("cpu_throttle", "mongodb", 30, 0.3, {"write": "fail"})
        with self.assertRaises(SafetyViolation):
            self.policy.validate_candidate(compose_compound(BURST, mongo, 10))
        unlisted = single("restart_container", "gateway", 10, 0.2, {"write": "degrade"})
        with self.assertRaises(SafetyViolation):
            self.policy.validate_candidate(compose_compound(BURST, unlisted, 10))

    def test_policy_file_can_disable_or_corrupt_compound(self):
        import dataclasses
        from safety.policy import CompoundPolicy
        off = dataclasses.replace(self.policy, compound=CompoundPolicy())
        with self.assertRaises(SafetyViolation):
            off.validate_candidate(self.good)
        raw = POLICY_PATH.read_text(encoding="utf-8")
        for bad in ("[traffic_burst:nginx, cpu_throttle:mongodb]", "[traffic_burst:nginx, run_shell:gateway]"):
            path = ROOT / "data" / "_tmp_policy.yaml"
            path.parent.mkdir(exist_ok=True)
            path.write_text(raw.replace("[traffic_burst:nginx, pause_container:things-search]", bad), encoding="utf-8")
            try:
                with self.subTest(bad), self.assertRaises(SafetyViolation):
                    Policy.from_file(path)
            finally:
                path.unlink(missing_ok=True)

    def test_single_fault_validation_unchanged(self):
        self.policy.validate_candidate({**PAUSE, "predicted_impacts": ["x"]})
        with self.assertRaises(SafetyViolation):
            self.policy.validate_candidate({**PAUSE, "target_component": "mongodb"})


class ComposeTests(unittest.TestCase):
    def test_compose_merges_worst_case_and_explains_interaction(self):
        compound = compose_compound(BURST, PAUSE, 10)
        self.assertEqual(compound["predicted_slis"], {"write": "degrade", "search": "fail", "freshness": "hold"})
        self.assertEqual(compound["predicted_interaction"]["shared_slis"], ["search"])
        self.assertEqual(compound["predicted_interaction"]["expectation"], "amplified")
        self.assertEqual([f["start_offset_seconds"] for f in compound["faults"]], [0, 10])
        self.assertIn("compound-of:" + BURST["id"], compound["evidence_refs"])

    def test_compose_is_deterministic_and_refuses_mixed_baselines_and_nesting(self):
        self.assertEqual(compose_compound(BURST, PAUSE, 10)["id"], compose_compound(BURST, PAUSE, 10)["id"])
        self.assertNotEqual(compose_compound(BURST, PAUSE, 10)["id"], compose_compound(BURST, PAUSE, 5)["id"])
        other = {**PAUSE, "evidence_refs": ["baseline:b2"]}
        with self.assertRaises(ValueError):
            compose_compound(BURST, other, 10)
        with self.assertRaises(ValueError):
            compose_compound(compose_compound(BURST, PAUSE, 10), PAUSE, 10)

    def test_merge_predictions_takes_worst(self):
        self.assertEqual(merge_predictions({"a": "hold"}, {"a": "fail"}, {"a": "degrade"}), {"a": "fail"})

    def test_suggestions_prefer_amplifying_pairs_and_respect_allowlist(self):
        policy = Policy.from_file(POLICY_PATH)
        quiet = single("cpu_throttle", "things-search", 45, 0.3, {"write": "hold", "search": "degrade", "freshness": "hold"})
        hold_burst = single("traffic_burst", "nginx", 45, 0.15, {"write": "hold", "search": "hold", "freshness": "degrade"})
        out = suggest_pairs([hold_burst, quiet, PAUSE], policy.compound.allowed_pairs)
        self.assertEqual({item["first"] for item in out}, {hold_burst["id"]})
        self.assertEqual(out[0]["expectation"], "independent")
        self.assertEqual(suggest_pairs([PAUSE, quiet], policy.compound.allowed_pairs), [])


class CompoundRunnerTests(unittest.TestCase):
    def setUp(self):
        self.policy = Policy.from_file(POLICY_PATH)
        self.clock = FakeClock()
        self.baseline = {"baseline_id": "b1", "telemetry_status": "available",
                         "captured_at": datetime.now(timezone.utc).isoformat(), "sli": {"write_p95_ms": 50.0}}
        self.candidate = compose_compound(BURST, PAUSE, 10)

    def runner(self, docker, observations, watchdog=None, recovery=None):
        return ExperimentRunner(self.policy, docker, observations, recovery or FakeRecovery(), watchdog or FakeWatchdog(),
                                clock=self.clock.monotonic, wall_clock=self.clock.time, sleep=self.clock.sleep)

    def approval(self, item):
        return {"decision": "approve", "candidate_hash": ExperimentRunner.candidate_hash(item),
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}

    def test_staged_run_injects_in_order_and_rolls_back_each_fault_at_its_end(self):
        docker, watchdog, obs = RecordingDocker(), FakeWatchdog(), RecordingObservations(HEALTHY)
        run = self.runner(docker, obs, watchdog).execute(self.candidate, self.approval(self.candidate), self.baseline)
        self.assertEqual(docker.order("apply"), ["traffic_burst", "pause_container"])
        # pause ends at 10+30=40 s, before the burst at 45 s
        self.assertEqual(docker.order("rollback"), ["pause_container", "traffic_burst"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["cleanup_state"], "complete")
        self.assertTrue(run["recovery_verified"])
        self.assertEqual(len(watchdog.armed), 2)
        self.assertEqual(watchdog.disarmed, 2)
        self.assertEqual([f["state"] for f in run["faults"]], ["rolled_back", "rolled_back"])
        # the pause rolled back 5 s before the burst, so it has 5 s more recovery time
        burst, pause = run["faults"]
        self.assertAlmostEqual(pause["recovery_after_own_rollback_seconds"] - burst["recovery_after_own_rollback_seconds"], 5.0, places=1)
        self.assertEqual(run["target_component"], "nginx+things-search")
        # every fault target is excluded from the "unrelated components healthy" check
        self.assertIn(("nginx", "things-search"), obs.targets)
        phases = {tuple(o.get("active_faults", [])) for o in run["observations"] if o["phase"] == "fault"}
        self.assertIn(("traffic_burst:nginx",), phases)
        self.assertIn(("traffic_burst:nginx", "pause_container:things-search"), phases)

    def test_kill_switch_abort_rolls_back_everything_in_reverse_order(self):
        docker = RecordingDocker()
        bad = {**HEALTHY, "write_error_rate": 0.5}
        obs = RecordingObservations(*[HEALTHY] * 6, bad, HEALTHY)  # abort at t=12, after both faults are active
        run = self.runner(docker, obs).execute(self.candidate, self.approval(self.candidate), self.baseline)
        self.assertEqual(run["status"], "aborted")
        self.assertEqual(run["abort_reason"], "error_rate_above_configured_limit")
        self.assertEqual(docker.order("rollback"), ["pause_container", "traffic_burst"])
        self.assertEqual(run["cleanup_state"], "complete")

    def test_abort_before_second_fault_never_injects_it(self):
        docker = RecordingDocker()
        bad = {**HEALTHY, "write_error_rate": 0.5}
        obs = RecordingObservations(HEALTHY, bad)  # first fault-phase poll aborts (t < 10 s)
        watchdog = FakeWatchdog()
        run = self.runner(docker, obs, watchdog).execute(self.candidate, self.approval(self.candidate), self.baseline)
        self.assertEqual(docker.order("apply"), ["traffic_burst"])
        self.assertEqual(docker.order("rollback"), ["traffic_burst"])
        self.assertEqual([f["state"] for f in run["faults"]], ["rolled_back", "planned"])
        self.assertEqual(watchdog.disarmed, 2)  # the never-injected fault's watchdog is disarmed too

    def test_one_failed_rollback_does_not_stop_the_other_and_keeps_its_watchdog_armed(self):
        docker, watchdog = RecordingDocker(fail_rollback_for="things-search"), FakeWatchdog()
        run = self.runner(docker, RecordingObservations(HEALTHY), watchdog).execute(
            self.candidate, self.approval(self.candidate), self.baseline)
        self.assertEqual(sorted(docker.order("rollback")), ["pause_container", "traffic_burst"])
        self.assertEqual(run["cleanup_state"], "failed")
        self.assertEqual(run["status"], "failed")
        self.assertNotIn("recovery_verified", run)
        self.assertEqual(watchdog.disarmed, 1)  # only the burst's; things-search stays armed
        self.assertEqual([f["state"] for f in run["faults"]], ["rolled_back", "rollback_failed"])

    def test_preflight_failure_injects_nothing(self):
        docker = RecordingDocker(fail_preflight=True)
        run = self.runner(docker, RecordingObservations(HEALTHY)).execute(
            self.candidate, self.approval(self.candidate), self.baseline)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(docker.order("apply"), [])
        self.assertEqual(docker.order("rollback"), [])

    def test_unhealthy_steady_state_refuses_before_any_fault(self):
        docker = RecordingDocker()
        run = self.runner(docker, RecordingObservations({**HEALTHY, "write_error_rate": 0.9})).execute(
            self.candidate, self.approval(self.candidate), self.baseline)
        self.assertEqual(run["status"], "aborted")
        self.assertTrue(run["abort_reason"].startswith("steady_state_not_met"))
        self.assertEqual(docker.order("apply"), [])

    def test_recovery_must_pass_for_every_target(self):
        class PerTarget:
            def verify(self, target):
                ok = target != "things-search"
                return {"healthy": True, "canary": ok, "target": target}
        self.policy = __import__("dataclasses").replace(
            self.policy, recovery_timeout_seconds=6.0, recovery_poll_interval_seconds=3.0)
        run = self.runner(RecordingDocker(), RecordingObservations(HEALTHY), recovery=PerTarget()).execute(
            self.candidate, self.approval(self.candidate), self.baseline)
        self.assertFalse(run["recovery_verified"])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(len(run["recovery"]["checks"]), 2)

    def test_approval_is_bound_to_every_fault(self):
        approval = self.approval(self.candidate)
        tampered = {**self.candidate, "faults": [self.candidate["faults"][0],
                                                 {**self.candidate["faults"][1], "action": "cpu_throttle"}]}
        with self.assertRaises(SafetyViolation):
            self.runner(RecordingDocker(), RecordingObservations(HEALTHY)).execute(tampered, approval, self.baseline)

    def test_dry_run_and_forced_test_are_never_scored(self):
        run = self.runner(RecordingDocker(), RecordingObservations(HEALTHY)).execute(
            self.candidate, self.approval(self.candidate), self.baseline, dry_run=True)
        self.assertEqual(run["resilience_score"]["status"], "not_scored_dry_run")

    def test_forced_test_proves_the_kill_switch_on_a_compound(self):
        docker = RecordingDocker()
        run = self.runner(docker, RecordingObservations(HEALTHY)).execute(
            self.candidate, self.approval(self.candidate), self.baseline, forced_test=True)
        self.assertTrue(run["forced_test_passed"])
        self.assertEqual(docker.order("rollback"), ["traffic_burst"] if docker.order("apply") == ["traffic_burst"] else ["pause_container", "traffic_burst"])

    def test_unrelated_health_excludes_all_targets(self):
        states = {"nginx": {"state": "exited"}, "things-search": {"state": "paused"}, "gateway": {"state": "running", "health": "healthy"}}
        self.assertFalse(unrelated_services_healthy(states, "things-search"))
        self.assertTrue(unrelated_services_healthy(states, ("nginx", "things-search")))


def solo_run(action, target, error=0.0, p95=100.0, run_id=None, status="completed"):
    return {"run_id": run_id or f"run-{action}", "status": status, "fault_type": action, "target_component": target,
            "action": action, "recovery_verified": True, "cleanup_state": "complete",
            "observations": [{"phase": "fault", "write_error_rate": error, "write_p95_ms": p95,
                              "search_error_rate": error, "freshness_samples": 5, "freshness_p95_seconds": 1.0}]}


def compound_run(error, p95):
    return {**solo_run("compound", "nginx+things-search", error, p95, "run-compound"), "kind": "compound",
            "faults": [{"action": "traffic_burst", "target": "nginx"}, {"action": "pause_container", "target": "things-search"}]}


class InteractionTests(unittest.TestCase):
    solos = [solo_run("traffic_burst", "nginx", 0.0, 200.0), solo_run("pause_container", "things-search", 0.1, 120.0)]

    def test_amplified_when_combination_is_worse_than_both_solos(self):
        effect = interaction_effect(compound_run(0.4, 900.0), self.solos)
        self.assertEqual(effect["verdict"], "amplified")
        self.assertEqual(effect["per_sli"]["write"]["error_rate"]["worst_solo"], 0.1)
        # a 0 -> 5 % blip is below the 10-point noise floor
        blip = interaction_effect(compound_run(0.05, 210.0), [solo_run("traffic_burst", "nginx", 0.0, 200.0), solo_run("pause_container", "things-search", 0.0, 200.0)])
        self.assertEqual(blip["verdict"], "no_interaction")
        finding = interaction_finding(compound_run(0.4, 900.0), effect)
        self.assertEqual(finding["severity"], "high")
        self.assertIn("solo:run-traffic_burst", finding["evidence"])

    def test_no_interaction_within_band_or_below_noise_floor(self):
        self.assertEqual(interaction_effect(compound_run(0.1, 210.0), self.solos)["verdict"], "no_interaction")
        # 0 -> 0.03 error rate is a huge ratio but below the absolute margin: not amplification
        quiet = [solo_run("traffic_burst", "nginx", 0.0, 100.0), solo_run("pause_container", "things-search", 0.0, 100.0)]
        self.assertEqual(interaction_effect(compound_run(0.03, 105.0), quiet)["verdict"], "no_interaction")
        self.assertIsNone(interaction_finding(compound_run(0.1, 210.0), interaction_effect(compound_run(0.1, 210.0), self.solos)))

    def test_dampened(self):
        solos = [solo_run("traffic_burst", "nginx", 0.0, 800.0), solo_run("pause_container", "things-search", 0.5, 120.0)]
        self.assertEqual(interaction_effect(compound_run(0.1, 200.0), solos)["verdict"], "dampened")

    def test_inconclusive_without_a_solo_run_for_every_fault(self):
        effect = interaction_effect(compound_run(0.4, 900.0), self.solos[:1])
        self.assertEqual(effect["verdict"], "inconclusive")
        self.assertIn("pause_container:things-search", effect["reason"])

    def test_solo_runs_in_real_runner_format_are_matched(self):
        # Real records have no top-level action/fault_type: only a diagnosis and an experiment id.
        def real(action, target, error, p95, run_id):
            run = solo_run(action, target, error, p95, run_id)
            for key in ("fault_type", "action", "target_component"):
                run.pop(key, None)
            run["experiment_id"] = f"{action}-{target}-abc"
            run["diagnosis"] = {"action": action, "target_component": target}
            return run
        solos = [real("traffic_burst", "nginx", 0.0, 200.0, "rb"), real("pause_container", "things-search", 0.1, 120.0, "rp")]
        effect = interaction_effect(compound_run(0.4, 900.0), solos)
        self.assertEqual(sorted(effect["solo_run_ids"]), ["rb", "rp"])
        self.assertEqual(effect["verdict"], "amplified")

    def test_dry_runs_forced_tests_and_dry_solos_are_ignored(self):
        self.assertEqual(interaction_effect({**compound_run(0.4, 900.0), "dry_run": True}, self.solos)["verdict"], "inconclusive")
        solos = [{**self.solos[0], "dry_run": True}, self.solos[1]]
        self.assertEqual(interaction_effect(compound_run(0.4, 900.0), solos)["verdict"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
