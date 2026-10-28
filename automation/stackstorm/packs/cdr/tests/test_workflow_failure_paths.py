"""Failure paths of the real workflow YAML, driven by Orquesta's own conductor (the engine st2 uses) with canned
task results. The point: whatever task fails or whichever branch ends the run, the capacity lease is released (or,
when this worker was fenced, deliberately left alone) - a lease left behind blocks every later capacity operation
until it expires, and then only reconcile may take it over (docs/07).

Needs `orquesta` (st2 3.9 ships 1.6.0; pip install --no-deps the v1.6.0 tag, plus networkx==3.1 yaql jinja2 stevedore
ujson jsonschema chardet). Without it this module fails at import - it is never silently skipped.
"""
import os

import pytest
import yaml
from orquesta import conducting, events, statuses
from orquesta.specs import native

WORKFLOWS = os.path.join(os.path.dirname(__file__), "..", "actions", "workflows")
GEN = 7
OP = "11111111-1111-4111-8111-111111111111"


def spec_of(name):
    with open(os.path.join(WORKFLOWS, name + ".yaml")) as f:
        spec = native.WorkflowSpec(f.read())
    spec.inspect()
    return spec


ACTIONS = {}
for _f in sorted(os.listdir(os.path.join(WORKFLOWS, ".."))):
    if _f.endswith(".yaml"):
        with open(os.path.join(WORKFLOWS, "..", _f)) as _h:
            _a = yaml.safe_load(_h)
        ACTIONS["cdr." + _a["name"]] = _a

PY_TYPES = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list, "number": (int, float)}


def check_input_types(action, given):
    """st2 validates a task's rendered input against the action's parameter schema before it runs, and an untyped
    parameter defaults to string. A mismatch stops the task from ever starting - which is how the failure handler
    once never ran (an object result passed to an untyped parameter)."""
    declared = (ACTIONS[action].get("parameters") or {}) if action in ACTIONS else {}
    for key, value in (given or {}).items():
        assert key in declared, f"{action}: undeclared parameter {key}"
        want = PY_TYPES[declared[key].get("type", "string")]
        if value is None:
            continue
        ok = isinstance(value, want) and not (want is int and isinstance(value, bool))
        assert ok, f"{action}.{key}: st2 would reject {type(value).__name__} for type {declared[key].get('type', 'string')}"


def py(result):
    """What a python-script action's execution result looks like to the workflow."""
    return {"result": result, "stdout": "", "stderr": "", "exit_code": 0}


def heavy_ok(given):
    """cdr.run_heavy: Terraform create (exists=true) answers with the resource, every other heavy task with a bare ok."""
    created = given["task"] == "terraform_apply" and given.get("exists") is True
    return py({"result": "ok", **({"resource_id": "sim-1", "ip": "192.0.2.1", "vcpu": 1, "memory_mb": 1024, "simulated": True} if created else {})})


HAPPY = {
    "cdr.adapter_info": py({"adapter_mode": "simulation", "simulated": True}),
    "cdr.update_ops_state": py({"sequence": 1}),
    "cdr.acquire_lease": py({"acquired": True, "generation": GEN, "previous_holder": None, "previous_expired": False}),
    "cdr.capacity_gate": py({"proceed": True, "metrics_source": "simulation"}),
    "cdr.next_elastic_provider": py({"provider": "linode", "skipped": [], "simulated": True, "adapter_mode": "simulation"}),
    "cdr.reserve_slot": py({"reserved": True, "slot_key": "elastic-linode-1", "slot_generation": 3, "instance_id": "i", "replayed": False}),
    "cdr.provision_and_deploy": {"output": {"result": "ready", "provider": "linode", "slot_key": "elastic-linode-1", "slot_generation": 3, "resource_id": "sim-linode-1", "attempts": {"linode": 1}}, "errors": []},
    "cdr.release_lease": py({"released": True}),
    "cdr.fail_operation": py({"status": "failed"}),
    "cdr.open_incident": py({"incident_id": "x", "created": True}),
    "cdr.capture_baseline": py({"recorded": False, "reason": "simulation"}),
    "core.noop": py({}),
}


def drive(name, inputs, results=None, fail=None, fail_all_of=()):
    """Run a workflow to its end. `results` overrides canned results per action ref; `fail` is a task name whose
    execution fails (once); returns (workflow status, [(task, action, rendered input)] in execution order)."""
    results = {**HAPPY, **(results or {})}
    results = {k: list(v) if isinstance(v, list) else v for k, v in results.items()}  # a list answers successive calls of the same action in order
    conductor = conducting.WorkflowConductor(spec_of(name), inputs=inputs, context={"st2": {"action_execution_id": "exec-1"}})
    conductor.request_workflow_status(statuses.RUNNING)
    ran = []
    for _ in range(200):
        tasks = conductor.get_next_tasks()
        if not tasks:
            break
        for t in tasks:
            tid, route = t["id"], t["route"]
            action = t["actions"][0]["action"]
            ran.append((tid, action, t["actions"][0]["input"]))
            if action in ACTIONS:
                check_input_types(action, t["actions"][0]["input"])
            conductor.update_task_state(tid, route, events.ActionExecutionEvent(statuses.RUNNING))
            if tid == fail or tid in fail_all_of:
                # what st2 hands the next task: a sub-workflow's {output, errors}, or a python action's result object
                failed_result = {"output": None, "errors": [{"message": "boom", "task_id": tid}]} if ACTIONS.get(action, {}).get("runner_type") == "orquesta" else {"stdout": "", "stderr": "", "exit_code": 0, "result": "connect_or_network"}
                ev = events.ActionExecutionEvent(statuses.FAILED, result=failed_result)
            else:
                answer = results[action]
                if callable(answer):
                    answer = answer(t["actions"][0]["input"])
                ev = events.ActionExecutionEvent(statuses.SUCCEEDED, result=answer.pop(0) if isinstance(answer, list) else answer)
            conductor.update_task_state(tid, route, ev)
    else:
        pytest.fail("workflow did not terminate")
    return conductor.get_workflow_status(), ran


SCALE_IN = {"operation_id": OP, "app_key": "relief"}


def names(ran):
    return [r[0] for r in ran]


def released_with_generation(ran, generation=GEN):
    """True when the run ends having released the lease (release task, or the failure handler that releases)."""
    for tid, action, inp in ran:
        if action == "cdr.release_lease" and inp["generation"] == generation:
            return True
        if action == "cdr.fail_operation" and inp.get("lease_generation") == generation:
            return True
    return False


def test_happy_path_runs_every_step_and_releases_the_lease():
    status, ran = drive("scale_out", SCALE_IN)
    assert status == statuses.SUCCEEDED
    assert names(ran)[-1] == "release" and released_with_generation(ran)
    assert names(ran).index("acquire") < names(ran).index("reserve") < names(ran).index("provision") < names(ran).index("release")


@pytest.mark.parametrize(
    "label, results, noop",
    [
        ("maintenance/cooldown/stale refusal", {"cdr.capacity_gate": py({"proceed": False, "noop_reason": "cooldown"})}, "cooldown"),
        ("no provider qualifies", {"cdr.next_elastic_provider": py({"provider": None, "skipped": [], "simulated": True, "adapter_mode": "simulation"})}, "no_capacity"),
        ("reserve_slot finds no capacity", {"cdr.reserve_slot": py({"reserved": False})}, "no_capacity"),
    ],
)
def test_every_no_op_branch_releases_the_lease(label, results, noop):
    status, ran = drive("scale_out", SCALE_IN, results)
    assert status == statuses.SUCCEEDED, label
    assert names(ran)[-1] == "release" and released_with_generation(ran), label
    assert "provision" not in names(ran)


@pytest.mark.parametrize("outcome", ["exhausted", "uncertain"])
def test_exhausted_and_uncertain_provisioning_end_failed_and_release_the_lease(outcome):
    sub = {"output": {"result": outcome, "provider": "linode", "slot_key": "elastic-linode-1", "slot_generation": 3, "resource_id": "", "attempts": {"linode": 2}}, "errors": []}
    status, ran = drive("scale_out", SCALE_IN, {"cdr.provision_and_deploy": sub})
    assert status == statuses.SUCCEEDED  # the operation fails, the execution that recorded it does not
    assert names(ran)[-1] == "release" and released_with_generation(ran)
    failed_event = [r for r in ran if r[1] == "cdr.update_ops_state" and r[2].get("event_type") == "operation.failed"]
    assert len(failed_event) == 1


HAPPY_TASKS = names(drive("scale_out", SCALE_IN)[1])
AFTER_LEASE = HAPPY_TASKS[HAPPY_TASKS.index("acquire") + 1:]


@pytest.mark.parametrize("task", [t for t in AFTER_LEASE if t != "release"])
def test_a_failing_task_after_the_lease_is_taken_reaches_the_handler_that_releases_it(task):
    status, ran = drive("scale_out", SCALE_IN, fail=task)
    handlers = [r for r in ran if r[0] == "fail_handler"]
    assert len(handlers) == 1, f"{task}: fail_handler ran {len(handlers)} times"
    assert handlers[0][2]["lease_generation"] == GEN, f"{task}: handler would not release generation {GEN}"
    assert names(ran)[-1] == "fail_handler" and released_with_generation(ran)
    # nothing with a side effect starts after the failure
    after = names(ran)[names(ran).index(task) + 1:]
    assert after == ["fail_handler"], f"{task}: ran {after} after failing"


@pytest.mark.parametrize("task", ["adapter", "start"])
def test_a_failure_before_the_lease_is_taken_has_no_generation_to_release(task):
    status, ran = drive("scale_out", SCALE_IN, fail=task)
    handler = [r for r in ran if r[0] == "fail_handler"][0]
    assert not handler[2]["lease_generation"]  # 0: nothing to release, and fail_operation then skips release_lease


def test_a_failed_acquire_is_a_recorded_no_op_and_takes_no_lease():
    status, ran = drive("scale_out", SCALE_IN, fail="acquire")
    assert names(ran)[-1] == "noop_no_lease" and not any(r[1] == "cdr.release_lease" for r in ran)


def test_the_release_and_the_failure_handler_are_retried_because_nothing_follows_them():
    spec = spec_of("scale_out")
    for task in ("release", "fail_handler"):
        retry = spec.tasks.get_task(task).retry
        assert retry and retry.count >= 2, f"{task} has no retry: one transient error would leave the lease held"


@pytest.mark.parametrize("name, inputs", [
    ("provision_attempt", {"operation_id": OP, "lease_generation": GEN, "provider": "linode", "slot_key": "elastic-linode-1", "slot_generation": 3, "attempt": 1}),
    ("provision_and_deploy", {"operation_id": OP, "lease_generation": GEN, "provider": "linode", "slot_key": "elastic-linode-1", "slot_generation": 3}),
])
def test_sub_workflows_stop_on_any_failure_so_the_parent_handler_runs(name, inputs):
    attempt_ready = {
        "cdr.start_attempt": py({"attempt_id": "a", "created": True, "deadline_at": "2026-01-01T00:00:00+00:00", "checkpoints_at": []}),
        "cdr.bind_slot": py({"bound": True}), "cdr.set_instance_state": py({"lifecycle": "x"}),
        "cdr.run_heavy": heavy_ok,
        "cdr.wait_ready": py({"result": "ready", "image_digest": "sha256:0", "identity": "sim-1"}),
        "cdr.finish_attempt": py({"status": "ready"}),
        "cdr.provision_attempt": {"output": {"result": "ready", "resource_id": "sim-1", "ip": "192.0.2.1"}, "errors": []},
    }
    status, ran = drive(name, inputs, attempt_ready)
    assert status == statuses.SUCCEEDED and ran
    for task in names(ran):
        st, after = drive(name, inputs, attempt_ready, fail=task)
        assert st == statuses.FAILED, f"{name}.{task}: workflow did not fail"
        assert names(after)[-1] == task, f"{name}.{task}: {names(after)[names(after).index(task) + 1:]} ran after the failure"


# ------------------------------------------------------------------ reconcile with unconfigured providers, and the restore probe
RECONCILE_RESULTS = {
    "cdr.open_operation": py({"operation_id": OP, "created": True, "policy_version": 1}),
    "cdr.control_state": py({}),
}


@pytest.mark.parametrize("noop_reason, ended_by", [("providers_unconfigured", "noop_unconfigured"), (None, "succeed")])
def test_reconcile_ends_as_a_noop_only_when_the_pass_reports_one_and_releases_the_lease_either_way(noop_reason, ended_by):
    summary = {"resumes": [], "orphans": [], **({"noop_reason": noop_reason} if noop_reason else {})}
    status, ran = drive("reconcile", {"app_key": "relief"}, {**RECONCILE_RESULTS, "cdr.reconcile_pass": py(summary)})
    assert status == statuses.SUCCEEDED and ended_by in names(ran) and ({"succeed", "noop_unconfigured"} - {ended_by}).isdisjoint(names(ran))
    assert released_with_generation(ran) and "dispatch" not in names(ran)
    if noop_reason:
        outcome = next(i for t, _a, i in ran if t == ended_by)["outcome"]
        assert outcome["noop_reason"] == "providers_unconfigured" and outcome["observed"]["noop_reason"] == "providers_unconfigured"


PROBE = {"cdr.open_operation": RECONCILE_RESULTS["cdr.open_operation"]}


def test_restore_probe_runs_the_heavy_task_under_the_lease_and_records_the_counts():
    counts = {"result": "ok", "task": "restore_probe", "tables_source": 4, "tables_restored": 4}
    status, ran = drive("restore_probe", {"app_key": "relief"}, {**PROBE, "cdr.run_heavy": py(counts)})
    assert status == statuses.SUCCEEDED and names(ran) == ["open", "start", "acquire", "heavy", "verifying", "succeed", "release"]  # succeeded is only valid from verifying
    heavy = next(i for t, _a, i in ran if t == "heavy")
    assert heavy == {"task": "restore_probe", "resource_class": "heavy", "operation_id": OP, "lease_generation": GEN}
    assert next(i for t, _a, i in ran if t == "succeed")["outcome"] == counts


@pytest.mark.parametrize("label, results, fail", [
    ("probe reports a failure", {"cdr.run_heavy": py({"result": "failed", "code": "restore_failed"})}, None),
    ("heavy action fails", {"cdr.run_heavy": py({})}, "heavy"),
    ("lease held elsewhere", {"cdr.acquire_lease": py({"acquired": False, "generation": 0})}, None),
])
def test_restore_probe_failures_reach_the_handler_with_the_generation_it_holds(label, results, fail):
    status, ran = drive("restore_probe", {"app_key": "relief"}, {**PROBE, **results}, fail=fail)
    assert "fail_handler" in names(ran) and "succeed" not in names(ran), label
    handler = next(i for t, _a, i in ran if t == "fail_handler")
    assert handler["lease_generation"] == (0 if label == "lease held elsewhere" else GEN)
