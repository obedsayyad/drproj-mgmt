"""Paths of the P4.4 workflows (standby_repair, apply_policy, the not_implemented skeletons) driven through Orquesta's conductor with canned task
results (same harness as test_workflow_failure_paths.py). What matters: which provider-side steps run for each classification (and which never do),
that an unknown answer creates nothing, that every failure reaches the one handler that releases the lease, that every task input has the type its
action declares, and that a skeleton can never end succeeded."""
import os

import pytest
from orquesta import statuses

from test_workflow_failure_paths import GEN, HAPPY, OP, WORKFLOWS, drive, names, py, released_with_generation

CHECKS = {"ready": True, "digest_matches": True, "instance_ready": True, "routing_backup": True}
EVIDENCE = {"confirmed_absent": True, "source": "provider_api", "observed_at": "2026-01-01T00:00:00Z", "simulated": True, "by": "standby_repair"}


def cls(klass, **kw):
    return py({"class": klass, "slot_key": "warm", "slot_generation": 4, "slot_state": "bound", "provider": "digitalocean", "resource_id": "sim-digitalocean-1",
               "interim_bound": False, "simulated": True, "other_provider": "linode", "other_available": True, **kw})


def sub_ready(kind_key="warm", provider="digitalocean"):
    return {"output": {"result": "ready", "provider": provider, "slot_key": kind_key, "slot_generation": 5, "resource_id": "sim-new-1", "attempts": {provider: 1}}, "errors": []}


SB = {
    **HAPPY,
    "cdr.wait_lease": py({"acquired": True, "generation": GEN, "previous_holder": None, "previous_expired": False}),
    "cdr.standby_classify": cls("healthy", image_digest="sha256:0"),
    "cdr.apply_routing": py({"applied": True, "target": "warm", "mode": "backup", "simulated": True}),
    "cdr.standby_window": py({"deadline_at": "2026-01-01T00:08:00+00:00", "checkpoints_at": ["2026-01-01T00:04:00+00:00", "2026-01-01T00:08:00+00:00"]}),
    "cdr.set_instance_state": py({"lifecycle": "ready"}),
    "cdr.run_heavy": py({"result": "ok"}),
    "cdr.wait_ready": py({"result": "ready", "image_digest": "sha256:0", "identity": "x"}),
    "cdr.provider_power_on": py({"result": "ok", "simulated": True}),
    "cdr.begin_slot_release": py({"releasing": True}),
    "cdr.release_slot": py({"released": True}),
    "cdr.reserve_slot": py({"reserved": True, "slot_key": "warm", "slot_generation": 5, "instance_id": "i", "replayed": False}),
    "cdr.provision_and_deploy": sub_ready(),
    "cdr.mark_slot_uncertain": py({"held_uncertain": True}),
    "cdr.standby_verify": py({"verified": True, "checks": CHECKS, "simulated": True}),
}
INPUT = {"operation_id": OP, "app_key": "relief"}


def run(results=None, **kw):
    return drive("standby_repair", INPUT, {**SB, **(results or {})}, **kw)


def ran_actions(ran):
    return [a for _n, a, _i in ran]


def events_of(ran):
    return [i["event_type"] for _n, a, i in ran if a == "cdr.update_ops_state"]


def last_outcome(ran, event):
    return [i for _n, a, i in ran if a == "cdr.update_ops_state" and i["event_type"] == event][-1]


PROVIDER_SIDE = ("cdr.provider_power_on", "cdr.run_heavy", "cdr.provision_and_deploy", "cdr.begin_slot_release", "cdr.release_slot", "cdr.reserve_slot", "cdr.mark_slot_uncertain")


def done_ok(ran, path):
    assert events_of(ran)[-2:] == ["standby.repaired", "operation.succeeded"], events_of(ran)
    assert last_outcome(ran, "operation.succeeded")["outcome"]["path"] == path
    assert names(ran)[-1] == "release" and released_with_generation(ran)
    assert names(ran).index("route_backup") < names(ran).index("verify") < names(ran).index("emit_repaired")  # done only after the verification


# ------------------------------------------------------------------ the four classifications of docs/04
def test_healthy_record_only_no_provider_side_step():
    status, ran = run()
    assert status == statuses.SUCCEEDED
    done_ok(ran, "healthy")
    assert not set(PROVIDER_SIDE) & set(ran_actions(ran))
    assert names(ran).index("route_maint") < names(ran).index("set_ready_healthy") < names(ran).index("route_backup")
    assert [i["mode"] for n, a, i in ran if a == "cdr.apply_routing"] == ["none", "backup"]


def test_a_repair_puts_the_standby_in_maintenance_before_anything_else_touches_it():
    status, ran = run()
    order = names(ran)
    assert order.index("classify") < order.index("route_maint") < order.index("decide_path")


def test_app_broken_redeploys_without_power_on_or_a_new_vm():
    status, ran = run({"cdr.standby_classify": cls("app_broken")})
    assert status == statuses.SUCCEEDED
    done_ok(ran, "redeploy")
    order = names(ran)
    assert order.index("window") < order.index("set_deploying") < order.index("deploy") < order.index("wait_ready") < order.index("set_ready") < order.index("route_backup")
    acts = ran_actions(ran)
    assert "cdr.provider_power_on" not in acts and "cdr.provision_and_deploy" not in acts and "cdr.reserve_slot" not in acts and "cdr.release_slot" not in acts


def test_powered_off_powers_on_then_redeploys():
    status, ran = run({"cdr.standby_classify": cls("powered_off")})
    assert status == statuses.SUCCEEDED
    done_ok(ran, "power_on_redeploy")
    assert names(ran).index("power_on") < names(ran).index("deploy")
    assert "cdr.provision_and_deploy" not in ran_actions(ran)


def test_power_on_that_gets_no_answer_holds_the_slot_instead_of_redeploying():
    status, ran = run({"cdr.standby_classify": [cls("powered_off"), cls("held_uncertain", slot_state="held_uncertain")], "cdr.provider_power_on": py({"result": "unknown", "simulated": True})})
    assert status == statuses.SUCCEEDED
    assert "deploy" not in names(ran) and names(ran).index("hold_uncertain") < names(ran).index("raise_uncertain")


def test_confirmed_absent_releases_with_the_evidence_then_reserves_and_provisions_the_same_warm_slot():
    status, ran = run({"cdr.standby_classify": cls("absent", evidence=EVIDENCE)})
    assert status == statuses.SUCCEEDED
    done_ok(ran, "reprovision")
    order = names(ran)
    assert order.index("begin_release") < order.index("release_slot") < order.index("reserve_warm") < order.index("provision_warm") < order.index("route_backup")
    rel = [i for n, a, i in ran if n == "release_slot"][0]
    assert rel["evidence"] == EVIDENCE and rel["slot_key"] == "warm" and rel["slot_generation"] == 4
    assert [i["kind"] for n, a, i in ran if a == "cdr.reserve_slot"] == ["warm"]
    assert [i["slot_kind"] for n, a, i in ran if a == "cdr.provision_and_deploy"] == ["warm"]


def test_a_free_warm_slot_is_reserved_and_provisioned_without_a_release():
    status, ran = run({"cdr.standby_classify": cls("absent", slot_state="free", evidence=None, resource_id="")})
    assert status == statuses.SUCCEEDED
    assert "begin_release" not in names(ran) and "release_slot" not in names(ran) and "provision_warm" in names(ran)


# ------------------------------------------------------------------ unknown: hold, incident, never create on the warm provider (T27d, T36)
UNKNOWN = {"cdr.standby_classify": [cls("unknown", reason="provider_answer_unknown"), cls("held_uncertain", slot_state="held_uncertain")]}


def test_unknown_with_the_other_provider_available_reserves_an_interim_warm_there_and_creates_nothing_on_the_warm_provider():
    results = {**UNKNOWN, "cdr.reserve_slot": py({"reserved": True, "slot_key": "interim-warm-linode", "slot_generation": 2, "instance_id": "i", "replayed": False}),
               "cdr.provision_and_deploy": sub_ready("interim-warm-linode", "linode")}
    status, ran = run(results)
    assert status == statuses.SUCCEEDED
    done_ok(ran, "interim")
    order = names(ran)
    assert order.index("hold_uncertain") < order.index("raise_uncertain") < order.index("interim_gate") < order.index("reserve_interim") < order.index("provision_interim") < order.index("route_backup")
    incident = [i for n, a, i in ran if a == "cdr.open_incident"][0]
    assert incident["fingerprint"] == "standby.uncertain:warm" and incident["severity"] == "critical"
    reserves = [(i["provider"], i["kind"]) for n, a, i in ran if a == "cdr.reserve_slot"]
    provisions = [(i["provider"], i["slot_kind"]) for n, a, i in ran if a == "cdr.provision_and_deploy"]
    assert reserves == [("linode", "interim_warm")] and provisions == [("linode", "interim_warm")]  # nothing on digitalocean


def test_unknown_with_no_other_provider_ends_failed_held_uncertain_and_creates_nothing_anywhere():
    results = {"cdr.standby_classify": [cls("unknown", reason="provider_answer_unknown"), cls("held_uncertain", slot_state="held_uncertain", other_available=False)]}
    status, ran = run(results)
    assert status == statuses.SUCCEEDED
    acts = ran_actions(ran)
    assert acts.count("cdr.mark_slot_uncertain") == 1 and "cdr.open_incident" in acts
    assert not {"cdr.reserve_slot", "cdr.provision_and_deploy", "cdr.run_heavy", "cdr.provider_power_on", "cdr.release_slot"} & set(acts)
    failed = last_outcome(ran, "operation.failed")
    assert failed["outcome"]["error"] == "held_uncertain" and names(ran)[-1] == "release" and released_with_generation(ran)


def test_unknown_for_a_free_warm_slot_raises_the_incident_and_does_not_mark_a_slot_that_is_not_occupied():
    results = {"cdr.standby_classify": [cls("unknown", slot_state="free", resource_id="", reason="provider_unavailable"), cls("absent", slot_state="free", evidence=None)]}
    status, ran = run(results)
    assert status == statuses.SUCCEEDED
    assert "cdr.mark_slot_uncertain" not in ran_actions(ran) and "cdr.reserve_slot" not in ran_actions(ran)
    assert last_outcome(ran, "operation.failed")["outcome"]["error"] == "held_uncertain"


def test_held_uncertain_with_an_interim_already_bound_is_a_noop():
    status, ran = run({"cdr.standby_classify": cls("held_uncertain", slot_state="held_uncertain", interim_bound=True)})
    assert status == statuses.SUCCEEDED
    assert last_outcome(ran, "operation.succeeded")["outcome"]["noop_reason"] == "interim_warm_active"
    assert not set(PROVIDER_SIDE) & set(ran_actions(ran)) and "cdr.apply_routing" not in ran_actions(ran)


@pytest.mark.parametrize("klass,reason", [("maintenance", "maintenance"), ("busy", "warm_slot_busy")])
def test_maintenance_and_a_busy_slot_are_noops_that_leave_routing_alone(klass, reason):
    status, ran = run({"cdr.standby_classify": cls(klass)})
    assert status == statuses.SUCCEEDED
    assert last_outcome(ran, "operation.succeeded")["outcome"]["noop_reason"] == reason
    assert not set(PROVIDER_SIDE) & set(ran_actions(ran)) and "cdr.apply_routing" not in ran_actions(ran)
    assert names(ran)[-1] == "release" and released_with_generation(ran)


@pytest.mark.parametrize("result,reason", [
    (py({"acquired": False, "reason": "timeout"}), "lease_timeout"),
    (py({"acquired": False, "reason": "superseded"}), "lease_superseded"),
])
def test_no_lease_means_no_step(result, reason):
    status, ran = run({"cdr.wait_lease": result})
    assert status == statuses.SUCCEEDED
    assert names(ran)[-1] == "noop_no_lease" and "cdr.release_lease" not in ran_actions(ran) and "cdr.standby_classify" not in ran_actions(ran)


# ------------------------------------------------------------------ one retry, then an incident; a failed repair never releases the slot
def test_a_redeploy_that_misses_the_deadline_is_tried_once_more():
    status, ran = run({"cdr.standby_classify": cls("app_broken"), "cdr.wait_ready": [py({"result": "deadline"}), py({"result": "ready", "image_digest": "sha256:0", "identity": "x"})]})
    assert status == statuses.SUCCEEDED
    done_ok(ran, "redeploy")
    assert names(ran).count("deploy") == 2 and names(ran).count("window") == 2


def test_two_missed_deadlines_raise_repair_failed_and_leave_the_slot_alone():
    status, ran = run({"cdr.standby_classify": cls("app_broken"), "cdr.wait_ready": py({"result": "deadline"})})
    assert status == statuses.SUCCEEDED
    assert names(ran).count("deploy") == 2
    assert [i["fingerprint"] for n, a, i in ran if a == "cdr.open_incident"] == ["standby.repair_failed:warm"]
    assert last_outcome(ran, "operation.failed")["outcome"]["error"] == "repair_failed" and "standby.repaired" not in events_of(ran)
    assert not {"cdr.begin_slot_release", "cdr.release_slot", "cdr.reserve_slot", "cdr.provision_and_deploy"} & set(ran_actions(ran))
    assert names(ran)[-1] == "release" and released_with_generation(ran)


def test_readiness_that_never_answers_holds_the_slot_and_considers_the_interim():
    status, ran = run({"cdr.standby_classify": [cls("app_broken"), cls("held_uncertain", slot_state="held_uncertain", other_available=False)], "cdr.wait_ready": py({"result": "unknown"})})
    assert status == statuses.SUCCEEDED
    assert names(ran).index("wait_ready") < names(ran).index("hold_uncertain") < names(ran).index("interim_gate") and "cdr.reserve_slot" not in ran_actions(ran)


def test_a_failed_verification_is_not_a_repair():
    status, ran = run({"cdr.standby_verify": py({"verified": False, "checks": {**CHECKS, "routing_backup": False}, "simulated": True})})
    assert status == statuses.SUCCEEDED
    assert "standby.repaired" not in events_of(ran) and last_outcome(ran, "operation.failed")["outcome"]["error"] == "repair_failed"


def test_exhausted_and_uncertain_provisioning_of_the_warm_slot_end_failed():
    exhausted = {"output": {"result": "exhausted", "provider": "digitalocean", "slot_key": "warm", "slot_generation": 5, "resource_id": "", "attempts": {"digitalocean": 2}}, "errors": []}
    status, ran = run({"cdr.standby_classify": cls("absent", evidence=EVIDENCE), "cdr.provision_and_deploy": exhausted})
    assert status == statuses.SUCCEEDED and last_outcome(ran, "operation.failed")["outcome"]["error"] == "repair_failed"
    uncertain = {"output": {"result": "uncertain", "provider": "digitalocean", "slot_key": "warm", "slot_generation": 5, "resource_id": "", "attempts": {"digitalocean": 1}}, "errors": []}
    status, ran = run({"cdr.standby_classify": cls("absent", evidence=EVIDENCE), "cdr.provision_and_deploy": uncertain})
    assert status == statuses.SUCCEEDED and last_outcome(ran, "operation.failed")["outcome"]["error"] == "held_uncertain"
    assert [i["fingerprint"] for n, a, i in ran if a == "cdr.open_incident"] == ["provision.uncertain:warm"]


# ------------------------------------------------------------------ any failing task ends in the handler that releases the lease
PATHS = {
    "healthy": {},
    "redeploy": {"cdr.standby_classify": cls("app_broken")},
    "power_on": {"cdr.standby_classify": cls("powered_off")},
    "reprovision": {"cdr.standby_classify": cls("absent", evidence=EVIDENCE)},
    "interim": {**UNKNOWN, "cdr.reserve_slot": py({"reserved": True, "slot_key": "interim-warm-linode", "slot_generation": 2, "instance_id": "i", "replayed": False}),
                "cdr.provision_and_deploy": sub_ready("interim-warm-linode", "linode")},
}


@pytest.mark.parametrize("path", sorted(PATHS))
def test_a_failing_task_after_the_lease_reaches_the_handler_that_releases_it(path):
    status, ran = run(PATHS[path])
    assert status == statuses.SUCCEEDED
    tasks = [(n, a) for n, a, _ in ran]
    after_lease = tasks[[n for n, _ in tasks].index("acquire") + 1:]
    for task, action in after_lease:
        if task == "release" or action.startswith("core."):
            continue
        st, after = run(PATHS[path], fail=task)
        handlers = [r for r in after if r[0] == "fail_handler"]
        assert len(handlers) == 1 and handlers[0][2]["lease_generation"] == GEN, f"{path}.{task}"
        assert names(after)[-1] == "fail_handler" and names(after)[names(after).index(task) + 1:] == ["fail_handler"], f"{path}.{task}: ran {names(after)[names(after).index(task) + 1:]}"


def test_the_release_and_the_failure_handler_are_retried():
    from test_workflow_failure_paths import spec_of

    spec = spec_of("standby_repair")
    for task in ("release", "fail_handler"):
        assert spec.tasks.get_task(task).retry.count >= 2


# ------------------------------------------------------------------ apply_policy
VALID = {"ok": True, "errors": [], "version": 2, "from_version": 1, "changed_paths": ["scale_out.cooldown_seconds"], "has_unimplemented": False, "capacity_changed": False, "unimplemented": {}}
AP = {
    **HAPPY,
    "cdr.policy_validate": py(VALID),
    "cdr.wait_lease": py({"acquired": True, "generation": GEN, "previous_holder": None, "previous_expired": False}),
    "cdr.policy_compile": py({"label": "2", "previous_label": "1", "mode": "simulation"}),
    "cdr.policy_verify": py({"loaded_label": "2"}),
    "cdr.policy_activate": py({"state": "active", "version": 2, "pending_transition": []}),
    "cdr.policy_rollback": py({"label_restored": True, "version_marked_failed": True, "active_version": 1}),
}


def apply(results=None, **kw):
    return drive("apply_policy", INPUT, {**AP, **(results or {})}, **kw)


def test_apply_policy_happy_path_without_a_lease():
    status, ran = apply()
    assert status == statuses.SUCCEEDED
    assert names(ran) == ["adapter", "start", "requested", "validate", "compile", "step_applied", "verify", "activate", "succeed"]
    assert events_of(ran) == ["operation.started", "policy.apply.requested", "operation.step.completed", "operation.succeeded"]
    assert "cdr.wait_lease" not in ran_actions(ran) and "cdr.release_lease" not in ran_actions(ran)
    assert [i["lease_generation"] for n, a, i in ran if a in ("cdr.policy_compile", "cdr.policy_activate")] == [0, 0]
    out = last_outcome(ran, "operation.succeeded")["outcome"]
    assert out["policy_version"] == 2 and out["compile_steps"]["prometheus_label"] == "applied" and out["compile_steps"]["haproxy"] == "unchanged"


def test_a_capacity_change_takes_the_exclusive_lease_and_releases_it():
    status, ran = apply({"cdr.policy_validate": py({**VALID, "capacity_changed": True})})
    assert status == statuses.SUCCEEDED
    order = names(ran)
    assert order.index("acquire") < order.index("compile") < order.index("activate") < order.index("succeed") < order.index("release")
    assert released_with_generation(ran)
    assert [i["lease_generation"] for n, a, i in ran if a in ("cdr.policy_compile", "cdr.policy_activate")] == [GEN, GEN]


def test_a_pending_transition_is_reported_in_the_outcome():
    transition = [{"provider": "linode", "occupied": 3, "cap": 2}]
    status, ran = apply({"cdr.policy_validate": py({**VALID, "capacity_changed": True}), "cdr.policy_activate": py({"state": "active", "version": 2, "pending_transition": transition})})
    assert status == statuses.SUCCEEDED and last_outcome(ran, "operation.succeeded")["outcome"]["pending_transition"] == transition


def test_a_change_whose_compile_target_is_a_later_phase_fails_not_implemented_and_applies_nothing():
    detail = {"haproxy": ["health.interval_seconds"]}
    status, ran = apply({"cdr.policy_validate": py({**VALID, "has_unimplemented": True, "unimplemented": detail})})
    assert status == statuses.SUCCEEDED
    assert not {"cdr.policy_compile", "cdr.policy_verify", "cdr.policy_activate", "cdr.wait_lease"} & set(ran_actions(ran))
    assert names(ran)[-3:] == ["validate", "rollback", "fail_handler"] or names(ran)[-2:] == ["rollback", "fail_handler"]
    handler = [i for n, a, i in ran if n == "fail_handler"][0]
    assert handler["error"] == {"code": "not_implemented", "detail": detail}
    assert "operation.succeeded" not in events_of(ran)


def test_an_invalid_pending_version_fails_policy_invalid():
    status, ran = apply({"cdr.policy_validate": py({**VALID, "ok": False, "errors": ["x"]})})
    assert status == statuses.SUCCEEDED and "cdr.policy_compile" not in ran_actions(ran)
    assert [i["error"]["code"] for n, a, i in ran if n == "fail_handler"] == ["policy_invalid"]


@pytest.mark.parametrize("task", ["validate", "compile", "step_applied", "verify", "activate"])
def test_any_failure_rolls_back_then_fails_the_operation(task):
    status, ran = apply(fail=task)
    assert status == statuses.SUCCEEDED
    after = names(ran)[names(ran).index(task) + 1:]
    assert after == ["rollback", "fail_handler"], f"{task}: {after}"
    assert "operation.succeeded" not in events_of(ran)


def test_a_failure_with_the_lease_held_releases_it():
    status, ran = apply({"cdr.policy_validate": py({**VALID, "capacity_changed": True})}, fail="verify")
    assert [i["lease_generation"] for n, a, i in ran if n == "fail_handler"] == [GEN] and names(ran)[-2:] == ["rollback", "fail_handler"]


# ------------------------------------------------------------------ skeletons
SKELETONS = {
    "start_load_test": ["validate_bounds", "start_k6", "observe"],
    "stop_load_test": ["stop_k6"],
    "create_backup": ["backup_lease", "pg_dump_capture", "record_artifact", "verify_after_capture", "expire_old"],
    "verify_backup": ["isolated_restore", "verify_contents", "record_result"],
    "restore_application": ["validate_verified_backup", "maintenance_on", "drain_writes", "safety_dump", "schema_swap", "verify_through_api", "maintenance_off"],
}


@pytest.mark.parametrize("name", sorted(SKELETONS))
def test_a_skeleton_workflow_declares_every_planned_step_as_a_not_implemented_task(name):
    from test_workflow_failure_paths import spec_of

    spec = spec_of(name)
    steps = [t for t, _ in spec.tasks if t not in ("start", "fail_handler")]
    assert steps == SKELETONS[name]
    for t in steps:
        assert spec.tasks.get_task(t).action == "cdr.not_implemented"


@pytest.mark.parametrize("name", sorted(SKELETONS))
def test_an_accidentally_triggered_skeleton_fails_not_implemented_and_touches_nothing(name):
    first = SKELETONS[name][0]
    status, ran = drive(name, {"operation_id": OP}, {"cdr.not_implemented": py("never read")}, fail=first)
    assert status == statuses.SUCCEEDED  # the execution ends; the operation is what is recorded failed
    assert names(ran) == ["start", first, "fail_handler"]
    handler = ran[-1][2]
    assert handler["step"] == first and not handler.get("lease_generation")
    acts = ran_actions(ran)
    assert set(acts) <= {"cdr.update_ops_state", "cdr.not_implemented", "cdr.fail_operation"}  # no lease, slot, routing or provider action
    assert events_of(ran) == ["operation.started"]


@pytest.mark.parametrize("name", sorted(SKELETONS))
def test_a_skeleton_workflow_never_records_success(name):
    # the step action always fails; this pins that nothing in the workflow file could turn that into a success event
    text = open(os.path.join(WORKFLOWS, name + ".yaml")).read()
    assert "operation.succeeded" not in text and "status: succeeded" not in text
