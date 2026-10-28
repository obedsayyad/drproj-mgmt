"""cdr.failback driven through Orquesta's conductor with canned task results (harness of test_workflow_failure_paths.py).
Real adapter: lease, direct probes, weight 0 + ready, wait UP, ramp 10/50/100 with observation gates, warm back to backup, incident resolved;
a regression reverses exactly one step and ends the operation failed (incident stays open). Simulation adapter: not_implemented before any lease."""
import pytest
from orquesta import statuses

from test_workflow_failure_paths import GEN, HAPPY, OP, drive, names, py

REAL = {
    **HAPPY,
    "cdr.adapter_info": py({"adapter_mode": "real", "simulated": False}),
    "cdr.failback_probe": py({"stable": True, "stable_seconds": 60, "adapter_mode": "real"}),
    "cdr.apply_routing": py({"applied": True, "target": "lb", "mode": "ramp", "simulated": False, "adapter_mode": "real"}),
    "cdr.failback_wait_up": py({"up": True, "adapter_mode": "real"}),
    "cdr.failback_observe": py({"passed": True, "failed_gates": [], "adapter_mode": "real"}),
    "cdr.resolve_incident": py({"resolved": True}),
}
INPUT = {"operation_id": OP}


def routing(ran):
    return [(i["target"], i["mode"], i.get("step_percent")) for t, a, i in ran if a == "cdr.apply_routing"]


def test_the_happy_path_ramps_10_50_100_then_returns_warm_to_backup_and_resolves_the_incident():
    status, ran = drive("failback", INPUT, REAL)
    assert status == statuses.SUCCEEDED
    assert names(ran) == ["adapter", "start", "acquire", "step_lease", "direct_probes", "weight_zero", "wait_up", "route_10", "ramp_10", "route_50", "ramp_50",
                          "route_100", "ramp_100", "route_backup", "verifying", "resolve", "succeed", "release"]
    assert routing(ran) == [("lb", "ramp", 0), ("lb", "ramp", 10), ("lb", "ramp", 50), ("lb", "normal", None), ("warm", "backup", None)]
    observed = [i["step_percent"] for t, a, i in ran if a == "cdr.failback_observe"]
    assert observed == [10, 50, 100]
    assert all(i["lease_generation"] == GEN for t, a, i in ran if a in ("cdr.apply_routing", "cdr.failback_probe", "cdr.failback_observe", "cdr.failback_wait_up"))
    assert [i["fingerprint"] for t, a, i in ran if a == "cdr.resolve_incident"] == ["primary.unhealthy:relief"]
    assert ran[-1][2]["generation"] == GEN


@pytest.mark.parametrize("step,back", [(10, 0), (50, 10), (100, 50)])
def test_a_regression_reverses_exactly_one_step_fails_the_operation_and_keeps_the_incident_open(step, back):
    res = lambda inp: {"passed": inp["step_percent"] != step, "failed_gates": [] if inp["step_percent"] != step else ["5xx"], "adapter_mode": "real"}
    status, ran = drive("failback", INPUT, {**REAL, "cdr.failback_observe": lambda inp: py(res(inp))})
    assert names(ran)[-2:] == [f"reverse_{back}", "regressed"]
    assert routing(ran)[-1] == ("lb", "ramp", back)
    handler = ran[-1][2]
    assert handler["error"]["code"] == "failback_regressed" and handler["lease_generation"] == GEN and handler["step"] == "reverse_on_regression"
    assert "cdr.resolve_incident" not in [a for t, a, i in ran] and "operation.succeeded" not in str([i for t, a, i in ran])


def test_a_primary_that_never_becomes_stable_is_a_noop_that_releases_the_lease_and_routes_nothing():
    status, ran = drive("failback", INPUT, {**REAL, "cdr.failback_probe": py({"stable": False, "adapter_mode": "real"})})
    assert names(ran)[-3:] == ["direct_probes", "noop_release", "release"] and routing(ran) == []
    assert [i["outcome"]["noop_reason"] for t, a, i in ran if t == "noop_release"] == ["primary_not_stable"]


def test_a_primary_that_does_not_come_up_after_ready_is_taken_back_to_weight_zero_and_fails():
    status, ran = drive("failback", INPUT, {**REAL, "cdr.failback_wait_up": py({"up": False, "adapter_mode": "real"})})
    assert names(ran)[-3:] == ["wait_up", "reverse_0", "regressed"]
    assert routing(ran)[-1] == ("lb", "ramp", 0)


def test_a_lease_held_by_another_operation_does_nothing():
    status, ran = drive("failback", INPUT, {**REAL, "cdr.acquire_lease": py({"acquired": False})})
    assert names(ran) == ["adapter", "start", "acquire", "noop_no_lease"] and routing(ran) == []


@pytest.mark.parametrize("task", ["direct_probes", "weight_zero", "wait_up", "route_10", "ramp_10", "route_50", "ramp_50", "route_100", "ramp_100", "route_backup", "resolve"])
def test_a_failing_step_reaches_the_one_handler_with_the_lease_generation_and_its_name(task):
    status, ran = drive("failback", INPUT, REAL, fail=task)
    assert ran[-1][0] == "fail_handler" and ran[-1][2]["lease_generation"] == GEN and ran[-1][2]["step"] == task
    assert "operation.succeeded" not in str([i for t, a, i in ran])


def test_with_the_simulation_adapter_failback_is_not_implemented_before_any_lease_or_routing():
    status, ran = drive("failback", INPUT, {**HAPPY}, fail="simulation_stub")
    assert names(ran) == ["adapter", "start", "simulation_stub", "fail_handler"]
    assert ran[-1][2]["step"] == "direct_probes" and not ran[-1][2].get("lease_generation")
    assert {a for t, a, i in ran} <= {"cdr.adapter_info", "cdr.update_ops_state", "cdr.not_implemented", "cdr.fail_operation"}
