"""Static checks on the pack's metadata and workflows (no st2 needed), so a typo is caught before the slow live run."""
import glob
import os
import re

import pytest
import yaml

PACK = os.path.join(os.path.dirname(__file__), "..")


def load(path):
    with open(os.path.join(PACK, path)) as f:
        return yaml.safe_load(f)


ACTIONS = {}
for _p in sorted(glob.glob(os.path.join(PACK, "actions", "*.yaml"))):
    _a = load(os.path.relpath(_p, PACK))
    ACTIONS[_a["name"]] = _a


def test_provider_adapter_is_required_and_has_no_default_and_faults_are_off_by_default():
    schema = load("config.schema.yaml")
    assert schema["provider_adapter"]["required"] is True and "default" not in schema["provider_adapter"]
    assert schema["provider_adapter"]["enum"] == ["simulation", "real"]
    assert schema["simulation_faults"].get("required") is False and "default" not in schema["simulation_faults"]
    assert not any("fault" in k and "default" in v for k, v in schema.items())


ST2_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}


def test_every_parameter_type_is_one_st2_accepts():
    for name, a in ACTIONS.items():
        assert isinstance(a.get("parameters"), dict), f"{name}: parameters must be a mapping (use {{}})"
        for pname, p in a["parameters"].items():
            assert "type" not in p or p["type"] in ST2_TYPES, f"{name}.{pname}: type {p.get('type')!r}"
            # unquoted on/off/yes/no are booleans in YAML 1.1 and silently break an enum of strings
            assert all(isinstance(v, str) for v in p.get("enum", [])), f"{name}.{pname}: enum {p.get('enum')!r} has non-string values"


def test_every_action_has_its_entry_point_and_a_matching_ref():
    for name, a in ACTIONS.items():
        assert a["pack"] == "cdr" and a["enabled"] is True, name
        assert os.path.exists(os.path.join(PACK, "actions", a["entry_point"])), name


@pytest.mark.parametrize("path", sorted(glob.glob(os.path.join(PACK, "actions", "workflows", "*.yaml"))))
def test_workflow_tasks_call_registered_actions_with_declared_parameters(path):
    wf = yaml.safe_load(open(path))
    inputs = {i if isinstance(i, str) else next(iter(i)) for i in wf["input"]}
    tasks = wf["tasks"]
    for name, t in tasks.items():
        ref = t["action"]
        if ref.startswith("core."):
            continue
        assert ref.startswith("cdr.") and ref[4:] in ACTIONS, f"{name}: unknown action {ref}"
        declared = ACTIONS[ref[4:]].get("parameters") or {}
        given = set(t.get("input") or {})
        assert given <= set(declared), f"{name}: undeclared parameters {given - set(declared)}"
        missing = {p for p, d in declared.items() if d.get("required") and "default" not in d} - given
        assert not missing, f"{name}: missing required parameters {missing}"
        for nxt in t.get("next") or []:
            for target in ([nxt["do"]] if isinstance(nxt.get("do"), str) else nxt.get("do") or []):
                assert target in tasks or target == "fail", f"{name}: transition to unknown task {target}"
    # every ctx() variable read is an input, a var, or published by some task
    published = set()
    for t in tasks.values():
        for nxt in t.get("next") or []:
            for p in nxt.get("publish") or []:
                published.add(next(iter(p)))
    known = inputs | {next(iter(v)) for v in wf.get("vars", [])} | published | {"st2"}
    text = open(path).read()
    used = set(re.findall(r"ctx\(\)\.(\w+)", text))
    assert used <= known, f"undefined ctx() names: {used - known}"


def test_workflow_actions_are_declared_as_orquesta_with_matching_inputs():
    for name in ("scale_out", "provision_and_deploy", "provision_attempt", "reconcile", "primary_failure", "scale_in", "activate_standby",
                 "standby_repair", "apply_policy",
                 "create_backup", "verify_backup", "restore_application", "failback", "start_load_test", "stop_load_test"):
        a = ACTIONS[name]
        assert a["runner_type"] == "orquesta" and a["entry_point"] == f"workflows/{name}.yaml"
        wf = load(f"actions/workflows/{name}.yaml")
        wf_inputs = {i if isinstance(i, str) else next(iter(i)) for i in wf["input"]}
        assert set(a["parameters"]) == wf_inputs


def test_policies_name_existing_actions_and_the_guard_keys_on_app_key():
    for path in glob.glob(os.path.join(PACK, "policies", "*.yaml")):
        p = yaml.safe_load(open(path))
        assert p["resource_ref"].startswith("cdr.") and p["resource_ref"][4:] in ACTIONS, path
    capacity_kinds = ("scale_out", "scale_in", "primary_failure", "activate_standby", "standby_repair", "apply_policy")
    for kind in capacity_kinds:
        guard = load(f"policies/capacity_guard_{kind}.yaml")
        assert guard["resource_ref"] == f"cdr.{kind}" and guard["name"] == f"capacity_guard.{kind}"
        assert guard["policy_type"] == "action.concurrency.attr" and guard["parameters"]["attributes"] == ["app_key"] and guard["parameters"]["threshold"] == 1
    # reconcile: overlapping ticks are cancelled per app_key (a delay guard would conflict with that)
    single = load("policies/reconcile_single.yaml")
    assert single["policy_type"] == "action.concurrency.attr" and single["resource_ref"] == "cdr.reconcile"
    assert single["parameters"] == {"action": "cancel", "threshold": 1, "attributes": ["app_key"]}
    heavy = load("policies/heavy.yaml")
    assert heavy["resource_ref"] == "cdr.run_heavy" and heavy["parameters"]["attributes"] == ["resource_class"]


def test_the_failure_handler_accepts_the_object_a_failed_task_returns():
    """Regression: `error` was untyped (st2 defaults to string), so st2 rejected every real failure result before
    fail_operation started, leaving the operation running and the lease held."""
    assert ACTIONS["fail_operation"]["parameters"]["error"]["type"] == "object"


def test_every_workflow_transition_states_its_condition():
    """A transition with no `when` fires when the task completes in ANY status, so a failed task carried on."""
    for path in glob.glob(os.path.join(PACK, "actions", "workflows", "*.yaml")):
        for name, t in yaml.safe_load(open(path))["tasks"].items():
            for nxt in t.get("next") or []:
                assert nxt.get("when"), f"{os.path.basename(path)}:{name} has a transition without `when`"


def test_alert_rule_runs_alert_intent_and_the_timer_runs_reconcile_every_60_s():
    assert load("rules/cdr_alert.yaml")["action"]["ref"] == "cdr.alert_intent"
    timer = load("rules/reconcile_every_60s.yaml")
    assert timer["action"]["ref"] == "cdr.reconcile" and timer["trigger"]["parameters"] == {"unit": "seconds", "delta": 60}
    assert "alert_intent" in ACTIONS and "reconcile" in ACTIONS


def test_db_only_actions_carry_a_60s_runner_timeout():
    """An action that only talks to the ops database must not outlive a stuck statement (cdr_db also bounds it server side)."""
    external = re.compile(r"^(import|from) (providers|st2_api|metrics|time|lease_keeper|cleanup)\b", re.M)
    checked = 0
    for name, a in ACTIONS.items():
        if a.get("runner_type") != "python-script":
            continue
        src = open(os.path.join(PACK, "actions", a["entry_point"])).read()
        if external.search(src):
            continue
        checked += 1
        assert a["parameters"].get("timeout", {}).get("default") == 60, f"{name}: DB-only action needs timeout default 60"
    assert checked >= 15


def test_run_heavy_is_the_only_terraform_path_and_its_timeout_covers_the_readiness_deadline():
    """docs/07: Terraform runs only inside cdr.run_heavy. Nothing else in the pack may call the Terraform adapter methods or spawn a process."""
    import json

    for path in glob.glob(os.path.join(PACK, "actions", "*.py")):
        text = open(path).read()
        name = os.path.basename(path)
        if name != "run_heavy.py":
            assert "tf_apply(" not in text and "tf_output(" not in text and "subprocess" not in text, f"{name} must not reach Terraform"
    heavy = ACTIONS["run_heavy"]["parameters"]
    assert heavy["task"]["enum"] == ["deploy", "terraform_apply", "terraform_output", "ansible_deploy"]
    policy = json.load(open(os.path.join(PACK, "..", "..", "..", "..", "docs", "config", "policy-defaults.json")))
    from run_heavy import ACTION_TIMEOUT_S, TF_MARGIN_S

    assert heavy["timeout"]["default"] == ACTION_TIMEOUT_S >= policy["provisioning"]["readiness_deadline_seconds"] + TF_MARGIN_S


def test_every_path_to_provider_cleanup_and_slot_delete_runs_the_terraform_delete_first():
    """Absence is proven after `apply exists=false`; a transition that skips tf_delete would prove it against a VM Terraform still owns."""
    for wf, target in (("provision_attempt", "cleanup"), ("scale_in", "delete")):
        tasks = load(f"actions/workflows/{wf}.yaml")["tasks"]
        entering = {name for name, t in tasks.items() for nxt in t.get("next") or [] if nxt.get("do") == target}
        assert entering == {"tf_delete"}, f"{wf}: {entering} reach {target} without tf_delete"
        assert tasks["tf_delete"]["action"] == "cdr.run_heavy" and tasks["tf_delete"]["input"]["exists"] is False
    create = load("actions/workflows/provision_attempt.yaml")["tasks"]["create"]
    assert create["action"] == "cdr.run_heavy" and create["input"]["task"] == "terraform_apply" and create["input"]["exists"] is True
