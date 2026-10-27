"""Paths of the P4.3 workflows (reconcile, primary_failure, scale_in, activate_standby) and of the resume/adopt additions,
driven through Orquesta's conductor with canned task results (same harness as test_workflow_failure_paths.py). What matters:
no side effect after a refusal or failure, the lease is released (or deliberately left alone), and every task input has the
type the action declares."""
import pytest
from orquesta import statuses

from test_workflow_failure_paths import GEN, OP, HAPPY, drive, heavy_ok, names, py, released_with_generation, spec_of

SLOT = {"slot_key": "elastic-linode-1", "slot_generation": 3, "provider": "linode", "resource_id": "sim-linode-1"}
P43 = {
    **HAPPY,
    "cdr.open_operation": py({"operation_id": OP, "created": True, "policy_version": 1}),
    "cdr.wait_lease": py({"acquired": True, "generation": GEN, "previous_holder": None, "previous_expired": False}),
    "cdr.supersede_queued": py({"superseded": []}),
    "cdr.apply_routing": py({"applied": True, "target": "warm", "mode": "active", "simulated": True}),
    "cdr.failover_target": py({"action": "reserved", "provider": "linode", "slot_key": "elastic-linode-1", "slot_generation": 3, "fallback": False}),
    "cdr.scale_in_gate": py({"proceed": True, **SLOT, "metrics_source": "simulation"}),
    "cdr.drain_instance": py({"result": "drained", "simulated": True}),
    "cdr.begin_slot_release": py({"releasing": True}),
    "cdr.run_heavy": heavy_ok,
    "cdr.delete_slot_resource": py({"result": "cleaned", "evidence": {"confirmed_absent": True, "source": "provider_api", "observed_at": "2026-01-01T00:00:00Z"}}),
    "cdr.release_slot": py({"released": True}),
    "cdr.mark_slot_uncertain": py({"held_uncertain": True}),
    "cdr.control_state": py({"was_down": False}),
    "cdr.reconcile_pass": py({"observe_only": False, "resumes": [], "adopted": []}),
    "cdr.dispatch": py({"dispatched": []}),
}
INPUT = {"operation_id": OP, "app_key": "relief"}


def run(name, inputs=None, results=None, **kw):
    return drive(name, inputs or INPUT, {**P43, **(results or {})}, **kw)


def side_effects(ran):
    return [n for n, a, _ in ran if a in ("cdr.begin_slot_release", "cdr.delete_slot_resource", "cdr.release_slot", "cdr.provision_and_deploy", "cdr.reserve_slot", "cdr.failover_target")]


# ------------------------------------------------------------------ scale_in
def test_scale_in_happy_path_drains_deletes_and_releases_the_slot_before_the_lease():
    status, ran = run("scale_in")
    assert status == statuses.SUCCEEDED
    order = names(ran)
    assert order.index("gate") < order.index("mark_draining") < order.index("drain") < order.index("route_off") < order.index("begin_release") < order.index("delete") < order.index("release_slot") < order.index("release")
    assert released_with_generation(ran)


@pytest.mark.parametrize("reason", ["primary_outage", "metrics_stale", "warm_not_ready", "no_candidate", "maintenance"])
def test_scale_in_refusals_are_noops_with_no_slot_or_provider_side_effect(reason):
    status, ran = run("scale_in", results={"cdr.scale_in_gate": py({"proceed": False, "noop_reason": reason})})
    assert status == statuses.SUCCEEDED
    assert side_effects(ran) == [] and "mark_draining" not in names(ran)
    noop = [r for r in ran if r[0] == "noop_release"][0]
    assert noop[2]["outcome"]["noop_reason"] == reason
    assert names(ran)[-1] == "release" and released_with_generation(ran)


def test_scale_in_drain_timeout_pauses_without_deleting_and_returns_the_server_to_service():
    status, ran = run("scale_in", results={"cdr.drain_instance": py({"result": "timeout", "last_state": "busy", "simulated": True})})
    assert status == statuses.SUCCEEDED
    order = names(ran)
    assert order[-1] == "release" and released_with_generation(ran)
    assert not {"route_off", "begin_release", "delete", "release_slot"} & set(order)
    assert order.index("drain_incident") < order.index("drain_event") < order.index("undrain") < order.index("pause")
    events = [r[2]["event_type"] for r in ran if r[1] == "cdr.update_ops_state"]
    assert "infrastructure.drain.timeout" in events and "operation.paused" in events
    assert [r[2]["mode"] for r in ran if r[0] == "undrain"] == ["active"]


@pytest.mark.parametrize("result", [{"result": "unknown", "reason": "get_unknown"}, {"result": "still_present", "resource_ids": ["sim-linode-1"]}])
def test_scale_in_unconfirmed_delete_holds_the_slot_and_never_releases_it(result):
    status, ran = run("scale_in", results={"cdr.delete_slot_resource": py(result)})
    assert status == statuses.SUCCEEDED
    assert "release_slot" not in names(ran)
    assert names(ran)[names(ran).index("delete"):][:3] == ["delete", "hold_uncertain", "raise_uncertain"]
    assert names(ran)[-1] == "release" and released_with_generation(ran)


def test_scale_in_superseded_while_waiting_ends_without_a_lease():
    status, ran = run("scale_in", results={"cdr.wait_lease": py({"acquired": False, "reason": "superseded"})})
    assert status == statuses.SUCCEEDED
    assert names(ran)[-1] == "noop_no_lease" and not any(r[1] == "cdr.release_lease" for r in ran) and side_effects(ran) == []
    assert [r for r in ran if r[0] == "noop_no_lease"][0][2]["outcome"]["noop_reason"] == "lease_superseded"


HAPPY_SI = names(run("scale_in")[1])


@pytest.mark.parametrize("task", [t for t in HAPPY_SI[HAPPY_SI.index("acquire") + 1:] if t != "release"])
def test_scale_in_task_failure_after_the_lease_reaches_the_releasing_handler(task):
    status, ran = run("scale_in", fail=task)
    assert names(ran)[-1] == "fail_handler" and released_with_generation(ran)
    assert names(ran)[names(ran).index(task) + 1:] == ["fail_handler"]


# ------------------------------------------------------------------ primary_failure
PF = {**INPUT, "resume": False, "first_attempt": 1, "attempts_done": {}}


def test_primary_failure_supersedes_then_routes_then_ensures_exactly_one_elastic():
    status, ran = run("primary_failure", PF)
    assert status == statuses.SUCCEEDED
    order = names(ran)
    assert order.index("supersede") < order.index("incident") < order.index("acquire") < order.index("route_primary") < order.index("route_warm") < order.index("ensure") < order.index("provision") < order.index("route_elastic")
    assert [r[2]["kind"] for r in ran if r[0] == "supersede"] == ["scale_in"]
    assert [(r[2]["target"], r[2]["mode"]) for r in ran if r[1] == "cdr.apply_routing"][:2] == [("primary", "maint"), ("warm", "active")]
    assert order.count("ensure") == 1 and released_with_generation(ran)


def test_primary_failure_reuses_an_existing_elastic_without_provisioning():
    status, ran = run("primary_failure", PF, {"cdr.failover_target": py({"action": "reuse", "slot_key": "elastic-do-1", "provider": "digitalocean", "slot_state": "bound", "held_by_operation": None})})
    assert status == statuses.SUCCEEDED
    assert "provision" not in names(ran) and names(ran)[-1] == "release" and released_with_generation(ran)


def test_primary_failure_resumes_in_its_own_slot_with_the_attempt_numbers_reconcile_passed():
    inputs = {**PF, "resume": True, "first_attempt": 2, "attempts_done": {"linode": 1}}
    status, ran = run("primary_failure", inputs, {"cdr.failover_target": py({"action": "resume", "provider": "linode", "slot_key": "elastic-linode-1", "slot_generation": 3, "slot_state": "bound"})})
    prov = [r for r in ran if r[0] == "provision"][0]
    assert status == statuses.SUCCEEDED and prov[2]["first_attempt"] == 2 and prov[2]["attempts_done"] == {"linode": 1}


def test_primary_failure_without_capacity_raises_an_incident_and_ends_a_recorded_noop():
    status, ran = run("primary_failure", PF, {"cdr.failover_target": py({"action": "none"})})
    assert status == statuses.SUCCEEDED
    assert names(ran)[-3:] == ["raise_no_capacity", "noop_release", "release"] and "provision" not in names(ran)


@pytest.mark.parametrize("outcome", ["exhausted", "uncertain"])
def test_primary_failure_exhausted_or_uncertain_provisioning_fails_the_operation_and_releases(outcome):
    sub = {"output": {"result": outcome, "provider": "linode", "slot_key": "elastic-linode-1", "slot_generation": 3, "resource_id": "", "attempts": {"linode": 2}}, "errors": []}
    status, ran = run("primary_failure", PF, {"cdr.provision_and_deploy": sub})
    assert status == statuses.SUCCEEDED and names(ran)[-1] == "release" and released_with_generation(ran)
    assert len([r for r in ran if r[1] == "cdr.update_ops_state" and r[2].get("event_type") == "operation.failed"]) == 1
    assert "route_elastic" not in names(ran)


HAPPY_PF = names(run("primary_failure", PF)[1])


@pytest.mark.parametrize("task", [t for t in HAPPY_PF[HAPPY_PF.index("acquire") + 1:] if t != "release"])
def test_primary_failure_task_failure_after_the_lease_reaches_the_releasing_handler(task):
    status, ran = run("primary_failure", PF, fail=task)
    assert names(ran)[-1] == "fail_handler" and released_with_generation(ran)
    assert names(ran)[names(ran).index(task) + 1:] == ["fail_handler"]


def test_primary_failure_lease_timeout_is_a_recorded_noop_and_does_not_provision():
    status, ran = run("primary_failure", PF, {"cdr.wait_lease": py({"acquired": False, "reason": "timeout"})})
    assert names(ran)[-1] == "noop_no_lease" and "ensure" not in names(ran)


# ------------------------------------------------------------------ activate_standby
def test_activate_standby_routes_the_warm_server_only_after_the_gate_passes():
    status, ran = run("activate_standby")
    assert status == statuses.SUCCEEDED and names(ran).index("gate") < names(ran).index("route_warm") and released_with_generation(ran)
    status, ran = run("activate_standby", results={"cdr.capacity_gate": py({"proceed": False, "noop_reason": "cooldown"})})
    assert "route_warm" not in names(ran) and names(ran)[-1] == "release"
    status, ran = run("activate_standby", results={"cdr.apply_routing": py({"applied": False})})
    assert [r for r in ran if r[0] == "noop_release"][0][2]["outcome"]["noop_reason"] == "no_warm_standby"


def test_activate_standby_takes_the_lease_once_and_a_live_holder_is_a_noop():
    status, ran = run("activate_standby", results={"cdr.acquire_lease": py({"acquired": False})})
    assert names(ran)[-1] == "noop_no_lease" and "gate" not in names(ran) and not any(r[1] == "cdr.release_lease" for r in ran)


# ------------------------------------------------------------------ reconcile
RC = {"app_key": "relief"}


def test_reconcile_happy_path_passes_under_the_lease_and_releases_it():
    status, ran = run("reconcile", RC)
    order = names(ran)
    assert status == statuses.SUCCEEDED
    assert order.index("recover") < order.index("acquire") < order.index("pass") < order.index("succeed") < order.index("release")
    assert "dispatch" not in order and "observe" not in order and released_with_generation(ran)


def test_reconcile_with_a_live_holder_only_observes_and_never_takes_or_releases_the_lease():
    status, ran = run("reconcile", RC, {"cdr.acquire_lease": py({"acquired": False})})
    assert status == statuses.SUCCEEDED
    obs = [r for r in ran if r[0] == "observe"][0]
    assert obs[2]["lease_generation"] == 0
    assert names(ran)[-1] == "noop_no_lease" and not any(r[1] == "cdr.release_lease" for r in ran) and "pass" not in names(ran)
    assert [r for r in ran if r[0] == "noop_no_lease"][0][2]["outcome"]["noop_reason"] == "lease_held"


def test_reconcile_passes_the_previous_holder_of_an_expired_lease_to_the_pass():
    status, ran = run("reconcile", RC, {"cdr.acquire_lease": py({"acquired": True, "generation": GEN, "previous_holder": "22222222-2222-4222-8222-222222222222", "previous_expired": True})})
    assert [r for r in ran if r[0] == "pass"][0][2]["previous_holder"] == "22222222-2222-4222-8222-222222222222"
    assert [r for r in run("reconcile", RC)[1] if r[0] == "pass"][0][2]["previous_holder"] == ""


def test_reconcile_dispatches_resumes_only_after_the_lease_is_released():
    resume = {"ref": "cdr.scale_out", "parameters": {"operation_id": OP, "resume": True, "first_attempt": 1, "attempts_done": {}}}
    status, ran = run("reconcile", RC, {"cdr.reconcile_pass": py({"observe_only": False, "resumes": [resume]})})
    assert names(ran)[-2:] == ["release", "dispatch"]
    assert [r for r in ran if r[0] == "dispatch"][0][2]["runs"] == [resume]


def test_reconcile_unreachable_lease_store_is_remembered_and_the_execution_fails():
    status, ran = run("reconcile", RC, fail="open")
    assert status == statuses.FAILED
    assert names(ran) == ["open", "control_down"] and [r for r in ran if r[0] == "control_down"][0][2] == {"mode": "down"}


@pytest.mark.parametrize("task", ["adapter", "start", "recover", "acquire", "pass", "verifying", "succeed"])
def test_reconcile_failure_reaches_the_handler_and_runs_nothing_after_it(task):
    status, ran = run("reconcile", RC, fail=task)
    assert names(ran)[-1] == "fail_handler" and names(ran)[names(ran).index(task) + 1:] == ["fail_handler"]


@pytest.mark.parametrize("name, retried", [("scale_in", ("release", "fail_handler")), ("primary_failure", ("release", "fail_handler")), ("activate_standby", ("release", "fail_handler")), ("reconcile", ("release", "fail_handler"))])
def test_terminal_tasks_are_retried_because_nothing_follows_them(name, retried):
    spec = spec_of(name)
    for task in retried:
        retry = spec.tasks.get_task(task).retry
        assert retry and retry.count >= 2, f"{name}.{task}"


# ------------------------------------------------------------------ scale_out resume and attempt adoption
def test_scale_out_resume_skips_gate_pick_and_reserve_and_continues_in_the_held_slot():
    inputs = {**INPUT, "resume": True, "first_attempt": 2, "attempts_done": {"linode": 1}}
    status, ran = run("scale_out", inputs, {"cdr.failover_target": py({"action": "resume", "provider": "linode", "slot_key": "elastic-linode-1", "slot_generation": 3, "slot_state": "bound"})})
    order = names(ran)
    assert status == statuses.SUCCEEDED and not {"gate", "pick", "reserve"} & set(order)
    assert [r for r in ran if r[0] == "provision"][0][2]["first_attempt"] == 2
    status, ran = run("scale_out", inputs, {"cdr.failover_target": py({"action": "none"})})
    assert [r for r in ran if r[0] == "noop_release"][0][2]["outcome"]["noop_reason"] == "resume_slot_missing" and "provision" not in names(ran)


ATTEMPT = {
    "cdr.start_attempt": py({"attempt_id": "a", "created": False, "deadline_at": "2026-01-01T00:00:00+00:00", "checkpoints_at": []}),
    "cdr.bind_slot": py({"bound": True}), "cdr.set_instance_state": py({"lifecycle": "x"}),
    "cdr.run_heavy": heavy_ok,
    "cdr.wait_ready": py({"result": "ready", "image_digest": "sha256:0", "identity": "sim-1"}),
    "cdr.finish_attempt": py({"status": "ready"}),
    "cdr.update_ops_state": py({"sequence": 1}),
    "cdr.provider_cleanup": py({"result": "cleaned", "evidence": {"confirmed_absent": True, "source": "provider_api", "observed_at": "2026-01-01T00:00:00Z"}}),
    "cdr.begin_slot_release": py({"releasing": True}), "cdr.release_slot": py({"released": True}),
    "cdr.mark_slot_uncertain": py({"held_uncertain": True}),
}
ATTEMPT_IN = {"operation_id": OP, "lease_generation": GEN, "provider": "linode", "slot_key": "elastic-linode-1", "slot_generation": 3, "attempt": 1}


@pytest.mark.parametrize(
    "adopt, expect_tasks, forbidden",
    [
        ({"result": "present", "resource_id": "sim-1", "ip": "192.0.2.1", "slot_state": "reserved"}, ["adopt", "step_created", "bind", "set_deploying", "deploy", "wait_ready"], ["create"]),
        ({"result": "present", "resource_id": "sim-1", "ip": "192.0.2.1", "slot_state": "bound"}, ["adopt", "set_deploying", "deploy", "wait_ready"], ["create", "bind"]),
        ({"result": "absent"}, ["adopt", "mark_failed", "emit_failed", "cleanup"], ["create", "deploy"]),
        ({"result": "failed"}, ["adopt", "cleanup"], ["create", "mark_failed"]),
        ({"result": "unknown", "reason": "list_unknown"}, ["adopt", "hold_uncertain", "finish_uncertain"], ["create", "cleanup", "deploy"]),
        ({"result": "ready"}, ["adopt"], ["create", "deploy"]),
    ],
)
def test_a_restarted_attempt_never_creates_again_and_follows_the_provider_answer(adopt, expect_tasks, forbidden):
    status, ran = drive("provision_attempt", ATTEMPT_IN, {**ATTEMPT, "cdr.adopt_attempt": py(adopt)})
    order = names(ran)
    assert status == statuses.SUCCEEDED
    assert order[0] == "start_attempt" and all(t in order for t in expect_tasks)
    assert not set(forbidden) & set(order)
    assert not any(a == "cdr.run_heavy" and i["task"] == "terraform_apply" and i["exists"] for _, a, i in ran)  # no Terraform create on a restarted attempt
