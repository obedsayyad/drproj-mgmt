"""The real provider adapter (P5.2). No network and no real Terraform: the provider HTTP APIs are mocked with `responses`, and
scripts/tf-slot.sh (the real script) drives tests/fixtures/fake-terraform.sh, which writes a state file like Terraform would.
Database-backed tests (run_heavy, fencing) use the same migrated Postgres as the other action tests."""
import copy
import json
import logging
import os

import pytest
import requests
import responses
import yaml
from jsonschema import Draft3Validator

import lease_keeper
import providers
from cleanup import absence_evidence
from providers.base import AdapterConfigError, tags_for
from providers.real import RealAdapter
from run_heavy import RunHeavy
from test_capacity_actions import db  # noqa: F401  (fixture reuse)
from bind_slot import BindSlot
from test_provisioning_actions import make, reserved, sim  # noqa: F401  (fixture reuse)

HERE = os.path.dirname(__file__)
SCRIPT = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", "..", "scripts", "tf-slot.sh"))
FAKE_TF = os.path.join(HERE, "fixtures", "fake-terraform.sh")
OP = "11111111-1111-4111-8111-111111111111"
DO_TOKEN, LI_TOKEN = "do-secret-token-5f1c", "li-secret-token-9a77"
DO, LI = "https://api.digitalocean.com", "https://api.linode.com"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("T_DO_TOKEN", DO_TOKEN)
    monkeypatch.setenv("T_LI_TOKEN", LI_TOKEN)
    monkeypatch.setenv("T_DO_KEY", "do-key-1")
    monkeypatch.setenv("T_LI_KEY", "li-key-1")
    state = tmp_path / "state"
    (state / ".fake").mkdir(parents=True)
    os.chmod(state, 0o700)
    cfg = {
        "tf_slot_script": SCRIPT, "tf_state_dir": str(state), "terraform_bin": FAKE_TF,
        "digitalocean": {"token_env": "env://T_DO_TOKEN", "region": "fra1", "size": "s-1vcpu-1gb", "image": "debian-12-x64", "ssh_key_id_env": "env://T_DO_KEY"},
        "linode": {"token_env": "env://T_LI_TOKEN", "region": "de-fra", "size": "g6-nanode-1", "image": "linode/debian12", "ssh_key_id_env": "env://T_LI_KEY"},
    }
    return {"cfg": cfg, "state": state, "ctl": state / ".fake"}


def fake_mode(env, *words):
    (env["ctl"] / "mode").write_text("\n".join(words) + "\n")


def apply_args(env):
    path = env["ctl"] / "apply_args"
    return path.read_text().splitlines() if path.exists() else []


def droplet(rid=424242, status="active", tags=None):
    return {"droplet": {"id": rid, "status": status, "vcpus": 1, "memory": 1024, "tags": tags or [],
                        "networks": {"v4": [{"type": "private", "ip_address": "10.0.0.2"}, {"type": "public", "ip_address": "203.0.113.9"}]}}}


def tag_list(slot="elastic-do-1", op=OP, attempt=1):
    return ["project:cdr", f"slot:{slot}", f"op:{op}", f"attempt:{attempt}"]


def mock_preflight_ok():
    responses.add(responses.GET, DO + "/v2/account", json={"account": {}}, status=200)


def create(adapter, provider="digitalocean", slot="elastic-do-1", attempt=1, spec=None):
    return adapter.create(provider, slot, OP, attempt, tags_for(slot, OP, attempt), {"warm_provider": "digitalocean", "timeout": 20, **(spec or {})})


# ------------------------------------------------------------------ create (Terraform apply, then output, then a provider get)
@responses.activate
def test_create_ok_runs_apply_then_output_then_get_and_never_sends_a_token_in_argv(env, caplog):
    caplog.set_level(logging.DEBUG)
    mock_preflight_ok()
    responses.add(responses.GET, DO + "/v2/droplets/424242", json=droplet(), status=200)
    adapter = RealAdapter(env["cfg"])
    res = create(adapter)
    assert res == {"result": "ok", "resource_id": "424242", "ip": "203.0.113.9", "vcpu": 1, "memory_mb": 1024}
    assert adapter.simulated is False and adapter.mode == "real"
    [args] = apply_args(env)
    for want in ("exists=true", f"op={OP}", "attempt=1", "region=fra1", "size=s-1vcpu-1gb", "image=debian-12-x64", "ssh_key_id=do-key-1",
                 "provider_name=digitalocean", "slot=elastic-do-1", "warm_provider=digitalocean", "-lock-timeout=30s"):
        assert want in args
    assert DO_TOKEN not in args and LI_TOKEN not in args
    assert all(DO_TOKEN not in json.dumps(v) and LI_TOKEN not in json.dumps(v) for v in (res, caplog.text))
    assert responses.calls[0].request.headers["Authorization"] == "Bearer " + DO_TOKEN
    assert (env["state"] / "digitalocean" / "elastic-do-1" / "terraform.tfstate").exists()


@pytest.mark.parametrize("status", [429, 401, 403])
@responses.activate
def test_create_refused_by_the_provider_is_unavailable_and_terraform_does_not_run(env, status):
    responses.add(responses.GET, DO + "/v2/account", json={}, status=status)
    assert create(RealAdapter(env["cfg"]))["result"] == "unavailable"
    assert apply_args(env) == []


@responses.activate
def test_create_with_an_unanswering_provider_is_unknown_and_terraform_does_not_run(env):
    responses.add(responses.GET, DO + "/v2/account", json={}, status=503)
    res = create(RealAdapter(env["cfg"]))
    assert res["result"] == "unknown" and res["reason"] == "preflight_unknown"
    assert apply_args(env) == []


@responses.activate
def test_create_timeout_is_ambiguous_never_absent(env):
    mock_preflight_ok()
    fake_mode(env, "sleep=4")
    res = create(RealAdapter(env["cfg"]), spec={"timeout": 1})
    assert res["result"] == "timeout_ambiguous" and res["reason"] == "terraform_timeout" and res["resource_id"] is None
    assert len(apply_args(env)) == 1


@responses.activate
def test_nonzero_exit_after_the_state_write_is_ambiguous_and_reports_the_resource_it_can_see(env):
    mock_preflight_ok()
    fake_mode(env, "fail_after_write")
    res = create(RealAdapter(env["cfg"]))
    assert res == {"result": "timeout_ambiguous", "reason": "terraform_failed", "resource_id": "424242"}


@responses.activate
def test_failure_after_apply_started_but_before_any_state_is_still_ambiguous(env):
    mock_preflight_ok()
    fake_mode(env, "fail_before_write")
    res = create(RealAdapter(env["cfg"]))
    assert res["result"] == "timeout_ambiguous" and res["resource_id"] is None


@responses.activate
def test_terraform_init_failure_happens_before_apply_started_so_nothing_was_touched(env):
    mock_preflight_ok()
    fake_mode(env, "fail_init")
    res = create(RealAdapter(env["cfg"]))
    assert res["result"] == "unknown" and res["reason"] == "terraform_not_started"
    assert apply_args(env) == []


@responses.activate
def test_the_wrapper_refusing_a_provider_slot_mismatch_is_not_started(env):
    responses.add(responses.GET, LI + "/v4/profile", json={}, status=200)
    res = create(RealAdapter(env["cfg"]), provider="linode", slot="elastic-do-1")
    assert res["result"] == "unknown" and res["reason"] == "terraform_not_started" and apply_args(env) == []


@responses.activate
def test_a_warm_slot_on_a_provider_other_than_the_warm_provider_is_refused_by_the_wrapper(env):
    responses.add(responses.GET, LI + "/v4/profile", json={}, status=200)
    res = create(RealAdapter(env["cfg"]), provider="linode", slot="warm", spec={"warm_provider": "digitalocean"})
    assert res["result"] == "unknown" and apply_args(env) == []


@pytest.mark.parametrize("get_status, reason", [(404, "get_absent"), (500, "get_unknown")])
@responses.activate
def test_a_resource_the_provider_cannot_confirm_after_apply_is_ambiguous_not_absent(env, get_status, reason):
    mock_preflight_ok()
    responses.add(responses.GET, DO + "/v2/droplets/424242", json={}, status=get_status)
    res = create(RealAdapter(env["cfg"]))
    assert res == {"result": "timeout_ambiguous", "reason": reason, "resource_id": "424242"}


@responses.activate
def test_missing_terraform_output_after_a_clean_apply_is_ambiguous(env):
    mock_preflight_ok()
    fake_mode(env, "fail_output")
    res = create(RealAdapter(env["cfg"]))
    assert res["result"] == "timeout_ambiguous" and res["reason"] == "terraform_output_missing"


# ------------------------------------------------------------------ delete = apply exists=false; absence is read, never assumed
@responses.activate
def test_delete_is_a_terraform_apply_and_the_adapter_delete_only_confirms_state_is_empty(env):
    mock_preflight_ok()
    responses.add(responses.GET, DO + "/v2/droplets/424242", json=droplet(), status=200)
    adapter = RealAdapter(env["cfg"])
    assert create(adapter)["result"] == "ok"
    assert adapter.delete("digitalocean", "424242") == {"result": "unknown", "reason": "terraform_state_not_empty"}
    assert adapter.tf_apply("digitalocean", "elastic-do-1", OP, 1, False, {"timeout": 20}) == {"result": "ok"}
    assert "exists=false" in apply_args(env)[-1]
    assert adapter.delete("digitalocean", "424242") == {"result": "ok"}
    assert len(responses.calls) == 2  # the preflight and the create-time get: delete and apply exists=false made no API call


@responses.activate
def test_a_failed_delete_apply_after_it_started_is_ambiguous(env):
    adapter = RealAdapter(env["cfg"])
    fake_mode(env, "fail_after_write")
    assert adapter.tf_apply("digitalocean", "elastic-do-1", OP, 1, False, {"timeout": 20})["result"] == "timeout_ambiguous"
    fake_mode(env, "fail_init")
    assert adapter.tf_apply("digitalocean", "elastic-do-1", OP, 1, False, {"timeout": 20})["result"] == "unknown"


def test_an_unreadable_state_file_is_never_evidence_of_deletion(env):
    path = env["state"] / "digitalocean" / "elastic-do-1"
    path.mkdir(parents=True)
    (path / "terraform.tfstate").write_text("{not json")
    adapter = RealAdapter(env["cfg"])
    assert adapter.delete("digitalocean", "424242") == {"result": "unknown", "reason": "terraform_state_unreadable"}
    assert adapter.slot_state_count("digitalocean", "elastic-do-1") is None


# ------------------------------------------------------------------ get / observe / power_on
@responses.activate
def test_get_maps_404_to_absent_and_everything_else_but_200_to_unknown(env):
    adapter = RealAdapter(env["cfg"])
    responses.add(responses.GET, DO + "/v2/droplets/1", json={"id": "not_found"}, status=404)
    responses.add(responses.GET, DO + "/v2/droplets/2", json={}, status=500)
    responses.add(responses.GET, DO + "/v2/droplets/3", json={}, status=429)
    responses.add(responses.GET, DO + "/v2/droplets/4", json={}, status=401)
    responses.add(responses.GET, DO + "/v2/droplets/5", body=requests.Timeout("slow"))
    responses.add(responses.GET, DO + "/v2/droplets/6", body="<html>", status=200)
    states = [adapter.get("digitalocean", str(i))["state"] for i in range(1, 7)]
    assert states == ["absent", "unknown", "unknown", "unknown", "unknown", "unknown"]
    responses.add(responses.GET, DO + "/v2/droplets/7", json=droplet(7, "off"), status=200)
    got = adapter.get("digitalocean", "7")
    assert got["state"] == "present" and got["power"] == "off" and got["ip"] == "203.0.113.9" and "observed_at" in got


@responses.activate
def test_linode_get_reports_power_and_specs_and_404_is_absent(env):
    adapter = RealAdapter(env["cfg"])
    responses.add(responses.GET, LI + "/v4/linode/instances/9", status=200,
                  json={"id": 9, "status": "offline", "ipv4": ["198.51.100.4"], "specs": {"vcpus": 1, "memory": 1024}, "tags": tag_list("elastic-linode-1")})
    responses.add(responses.GET, LI + "/v4/linode/instances/10", json={"errors": [{"reason": "Not found"}]}, status=404)
    got = adapter.get("linode", "9")
    assert (got["state"], got["power"], got["ip"], got["vcpu"], got["memory_mb"]) == ("present", "off", "198.51.100.4", 1, 1024)
    assert adapter.get("linode", "10")["state"] == "absent"
    assert responses.calls[0].request.headers["Authorization"] == "Bearer " + LI_TOKEN


@pytest.mark.parametrize("status, want", [(200, "available"), (401, "unavailable"), (403, "unavailable"), (429, "unavailable"), (500, "unknown")])
@responses.activate
def test_observe(env, status, want):
    responses.add(responses.GET, DO + "/v2/account", json={}, status=status)
    assert RealAdapter(env["cfg"]).observe("digitalocean") == want


@responses.activate
def test_observe_unreachable_is_unknown(env):
    responses.add(responses.GET, LI + "/v4/profile", body=requests.ConnectionError("down"))
    assert RealAdapter(env["cfg"]).observe("linode") == "unknown"


@responses.activate
def test_list_by_tag_needs_all_four_tags_and_a_partial_match_is_not_a_match(env):
    adapter = RealAdapter(env["cfg"])
    other_op = "22222222-2222-4222-8222-222222222222"
    droplets = [
        {**droplet(1)["droplet"], "tags": tag_list()},                              # all four
        {**droplet(2)["droplet"], "tags": tag_list(attempt=2)},                     # attempt differs
        {**droplet(3)["droplet"], "tags": tag_list(op=other_op)},                   # op differs
        {**droplet(4)["droplet"], "tags": tag_list(slot="elastic-do-2")},           # slot differs
        {**droplet(5)["droplet"], "tags": ["project:cdr", "slot:elastic-do-1"]},    # op and attempt missing
        {**droplet(6)["droplet"], "tags": []},
    ]
    responses.add(responses.GET, DO + "/v2/droplets", json={"droplets": droplets, "links": {}}, status=200)
    got = adapter.list_by_tag("digitalocean", tags_for("elastic-do-1", OP, 1))
    assert got["state"] == "ok" and [r["resource_id"] for r in got["resources"]] == ["1"]
    assert "tag_name=project%3Acdr" in responses.calls[0].request.url or "tag_name=project:cdr" in responses.calls[0].request.url
    slot_only = adapter.list_by_tag("digitalocean", {"project": "cdr", "slot": "elastic-do-1"})
    assert [r["resource_id"] for r in slot_only["resources"]] == ["1", "2", "3", "5"]


@responses.activate
def test_list_by_tag_follows_pages_and_filters_linode_by_the_project_tag_server_side(env):
    adapter = RealAdapter(env["cfg"])
    mine = {"id": 11, "status": "running", "ipv4": ["198.51.100.1"], "specs": {}, "tags": tag_list("elastic-linode-1")}
    stray = {"id": 12, "status": "running", "ipv4": [], "specs": {}, "tags": ["project:cdr", "slot:elastic-linode-1"]}
    responses.add(responses.GET, LI + "/v4/linode/instances", json={"data": [stray], "page": 1, "pages": 2}, status=200)
    responses.add(responses.GET, LI + "/v4/linode/instances", json={"data": [mine], "page": 2, "pages": 2}, status=200)
    got = adapter.list_by_tag("linode", tags_for("elastic-linode-1", OP, 1))
    assert got["state"] == "ok" and [r["resource_id"] for r in got["resources"]] == ["11"]
    assert json.loads(responses.calls[0].request.headers["X-Filter"]) == {"tags": "project:cdr"}


@pytest.mark.parametrize("answer", [{"status": 500}, {"status": 429}, {"body": requests.Timeout("slow")}, {"status": 200, "body": "not json"}])
@responses.activate
def test_list_by_tag_that_fails_is_unknown_never_empty(env, answer):
    responses.add(responses.GET, DO + "/v2/droplets", **answer)
    assert RealAdapter(env["cfg"]).list_by_tag("digitalocean", {"project": "cdr"}) == {"state": "unknown", "resources": []}


@pytest.mark.parametrize("status, want", [(201, "ok"), (403, "unavailable"), (429, "unavailable"), (500, "unknown")])
@responses.activate
def test_power_on_digitalocean(env, status, want):
    responses.add(responses.POST, DO + "/v2/droplets/424242/actions", json={}, status=status)
    assert RealAdapter(env["cfg"]).power_on("digitalocean", "424242") == {"result": want}
    assert json.loads(responses.calls[0].request.body) == {"type": "power_on"}


@responses.activate
def test_power_on_linode_and_a_timeout_is_unknown(env):
    responses.add(responses.POST, LI + "/v4/linode/instances/9/boot", json={}, status=200)
    responses.add(responses.POST, LI + "/v4/linode/instances/10/boot", body=requests.Timeout("slow"))
    adapter = RealAdapter(env["cfg"])
    assert adapter.power_on("linode", "9") == {"result": "ok"}
    assert adapter.power_on("linode", "10") == {"result": "unknown"}


def test_a_token_environment_variable_that_is_not_set_is_a_configuration_error(env, monkeypatch):
    monkeypatch.delenv("T_DO_TOKEN")
    with pytest.raises(AdapterConfigError, match="real_token_missing"):
        RealAdapter(env["cfg"]).observe("digitalocean")


# ------------------------------------------------------------------ per-provider token scoping (each provider has its own Terraform root)
def tf_env_log(env):
    p = env["ctl"] / "tf_env"
    return p.read_text().splitlines() if p.exists() else []


@responses.activate
def test_digitalocean_create_and_delete_succeed_with_the_linode_token_unset(env, monkeypatch):
    monkeypatch.delenv("T_LI_TOKEN")
    mock_preflight_ok()
    responses.add(responses.GET, DO + "/v2/droplets/424242", json=droplet(), status=200)
    adapter = RealAdapter(env["cfg"])
    assert create(adapter)["result"] == "ok"
    assert adapter.tf_apply("digitalocean", "elastic-do-1", OP, 2, False, {"timeout": 20}) == {"result": "ok"}
    assert any("exists=false" in a for a in apply_args(env))


@responses.activate
def test_linode_create_and_delete_succeed_with_the_digitalocean_token_unset(env, monkeypatch):
    monkeypatch.delenv("T_DO_TOKEN")
    responses.add(responses.GET, LI + "/v4/profile", json={}, status=200)
    responses.add(responses.GET, LI + "/v4/linode/instances/424242", json={"id": 424242, "status": "running", "ipv4": ["203.0.113.9"],
                                                                         "specs": {"vcpus": 1, "memory": 1024}, "tags": tag_list("elastic-linode-1")}, status=200)
    adapter = RealAdapter(env["cfg"])
    assert create(adapter, "linode", "elastic-linode-1", spec={"warm_provider": "linode"})["result"] == "ok"
    assert adapter.tf_apply("linode", "elastic-linode-1", OP, 2, False, {"timeout": 20}) == {"result": "ok"}
    assert (env["state"] / "linode" / "elastic-linode-1" / "terraform.tfstate").exists()


@pytest.mark.parametrize("provider,slot,var,exists", [
    ("digitalocean", "elastic-do-1", "T_DO_TOKEN", True), ("digitalocean", "elastic-do-1", "T_DO_TOKEN", False),
    ("linode", "elastic-linode-1", "T_LI_TOKEN", True), ("linode", "elastic-linode-1", "T_LI_TOKEN", False),
])
@responses.activate
def test_a_missing_token_for_the_active_provider_is_unavailable_and_terraform_does_not_run(env, monkeypatch, provider, slot, var, exists):
    monkeypatch.delenv(var)
    res = RealAdapter(env["cfg"]).tf_apply(provider, slot, OP, 1, exists, {"timeout": 20})
    assert res["result"] == "unavailable" and res["reason"] == "provider_token_missing"
    assert apply_args(env) == [] and not (env["state"] / provider).exists()
    assert len(responses.calls) == 0


def test_the_runner_environment_carries_only_the_active_providers_token(env):
    from providers.tf_runner import TfRunner

    r = TfRunner(SCRIPT, str(env["state"]), {"digitalocean": "env://T_DO_TOKEN", "linode": "env://T_LI_TOKEN"})
    do_env, li_env = r._env("digitalocean", "elastic-do-1"), r._env("linode", "elastic-linode-1")
    assert do_env["DIGITALOCEAN_TOKEN"] == DO_TOKEN and "LINODE_TOKEN" not in do_env and LI_TOKEN not in do_env.values()
    assert li_env["LINODE_TOKEN"] == LI_TOKEN and "DIGITALOCEAN_TOKEN" not in li_env and DO_TOKEN not in li_env.values()


# ------------------------------------------------------------------ absence evidence (release_slot in real mode)
def prove(env, *, state_has_resource=False):
    adapter = RealAdapter(env["cfg"])
    if state_has_resource:
        fake_mode(env)
        mock_preflight_ok()
        responses.add(responses.GET, DO + "/v2/droplets/424242", json=droplet(), status=200)  # consumed by the create's own get
        assert adapter.tf_apply("digitalocean", "elastic-do-1", OP, 1, True, {"timeout": 20})["result"] == "ok"
    else:
        adapter.tf_apply("digitalocean", "elastic-do-1", OP, 1, False, {"timeout": 20})
    return adapter


def slot_listing(resources=()):
    responses.add(responses.GET, DO + "/v2/droplets", json={"droplets": list(resources), "links": {}}, status=200)


@responses.activate
def test_absence_needs_empty_state_and_get_absent_and_an_empty_tag_listing(env):
    adapter = prove(env)
    responses.add(responses.GET, DO + "/v2/droplets/424242", json={}, status=404)
    slot_listing()
    evidence, why = absence_evidence(adapter, "digitalocean", "elastic-do-1", ["424242"], by="test")
    assert why is None and evidence["confirmed_absent"] is True and evidence["source"] == "provider_api" and evidence["simulated"] is False
    assert evidence["checks"] == {"slot_state_resources": 0, "provider_get": "absent", "list_by_tag": 0} and evidence["resource_ids"] == ["424242"]


@responses.activate
def test_absence_is_refused_while_the_slot_state_still_holds_a_resource(env):
    adapter = prove(env, state_has_resource=True)
    responses.add(responses.GET, DO + "/v2/droplets/424242", json={}, status=404)
    slot_listing()
    assert absence_evidence(adapter, "digitalocean", "elastic-do-1", ["424242"]) == (None, "slot_state_not_empty")


@pytest.mark.parametrize("get_status, want", [(200, "get_present"), (500, "get_unknown"), (429, "get_unknown")])
@responses.activate
def test_absence_is_refused_unless_the_provider_get_says_absent(env, get_status, want):
    adapter = prove(env)
    responses.add(responses.GET, DO + "/v2/droplets/424242", json=droplet(), status=get_status)
    slot_listing()
    assert absence_evidence(adapter, "digitalocean", "elastic-do-1", ["424242"]) == (None, want)


@responses.activate
def test_absence_is_refused_when_the_tag_listing_is_not_empty_or_does_not_answer(env):
    adapter = prove(env)
    responses.add(responses.GET, DO + "/v2/droplets/424242", json={}, status=404)
    slot_listing([{**droplet(9)["droplet"], "tags": tag_list()}])
    assert absence_evidence(adapter, "digitalocean", "elastic-do-1", ["424242"]) == (None, "list_not_empty")
    responses.reset()
    responses.add(responses.GET, DO + "/v2/droplets/424242", json={}, status=404)
    responses.add(responses.GET, DO + "/v2/droplets", json={}, status=500)
    assert absence_evidence(adapter, "digitalocean", "elastic-do-1", ["424242"]) == (None, "list_unknown")


def test_absence_is_refused_when_the_state_cannot_be_read(env):
    adapter = RealAdapter(env["cfg"])
    path = env["state"] / "digitalocean" / "elastic-do-1"
    path.mkdir(parents=True)
    (path / "terraform.tfstate").write_text("garbage")
    assert absence_evidence(adapter, "digitalocean", "elastic-do-1", []) == (None, "slot_state_unreadable")


# ------------------------------------------------------------------ run_heavy with the real adapter (real Postgres, fenced)
def real_heavy(sim, env):
    return make(RunHeavy, sim, provider_adapter="real", real_adapter=env["cfg"])


@responses.activate
def test_run_heavy_terraform_apply_renews_the_lease_during_a_slow_apply_and_passes_the_warm_provider(db, sim, env, monkeypatch):
    op, gen, key, _ = reserved(db, sim)
    mock_preflight_ok()
    responses.add(responses.GET, DO + "/v2/droplets/424242", json=droplet(), status=200)
    fake_mode(env, "sleep=3")
    renewals = []
    real_renew = lease_keeper._renew_via_db
    monkeypatch.setattr(lease_keeper, "_renew_via_db", lambda *a, **k: renewals.append(1) or real_renew(*a, **k))
    import run_heavy as rh

    real_section = rh.policy_section
    monkeypatch.setattr(rh, "policy_section", lambda c, o, s: (True, {**real_section(c, o, s)[1], "lease_renewal_seconds": 1}) if s == "coordination" else real_section(c, o, s))
    ok, res = real_heavy(sim, env).run("terraform_apply", "heavy", op, gen, "digitalocean", slot_key=key, attempt=1, exists=True)
    assert ok and res["result"] == "ok" and res["resource_id"] == "424242" and res["simulated"] is False and res["task"] == "terraform_apply"
    assert len(renewals) >= 2
    assert "warm_provider=digitalocean" in apply_args(env)[0]


@responses.activate
def test_a_fenced_worker_runs_neither_terraform_nor_a_provider_call(db, sim, env):
    op, gen, key, _ = reserved(db, sim)
    mock_preflight_ok()
    assert real_heavy(sim, env).run("terraform_apply", "heavy", op, gen + 1, "digitalocean", slot_key=key, attempt=1, exists=True) == (False, "lease_lost")
    assert apply_args(env) == [] and len(responses.calls) == 0


def test_real_without_its_settings_is_a_config_error(db, sim, env):
    op, gen, key, _ = reserved(db, sim)
    assert make(RunHeavy, sim, provider_adapter="real").run("terraform_apply", "heavy", op, gen, "digitalocean", slot_key=key, exists=True) == (False, "provider_adapter_missing_or_unknown")
    assert apply_args(env) == []


# ------------------------------------------------------------------ ansible_deploy (P5.3): a fake ansible-playbook, no host contacted
FAKE_ANSIBLE = os.path.join(HERE, "fixtures", "fake-ansible-playbook.sh")
DIGEST = "sha256:" + "a" * 64
RID = "424242"


@pytest.fixture()
def ans(tmp_path, env, monkeypatch):
    """Fake ansible-playbook beside its control files; the deployment inputs name the env refs the runner may pass through."""
    bindir = tmp_path / "ansbin"
    bindir.mkdir()
    (tmp_path / "ansible").mkdir()
    fake = bindir / "ansible-playbook"
    fake.write_text(open(FAKE_ANSIBLE).read())
    fake.chmod(0o755)
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps({"supabase": {"app_connection_reference": "env://SUPABASE_APP_URL"}, "registry": {"pull_credential_reference": "env://GHCR_READONLY_TOKEN"},
                                  "tls": {"backend_trust_bundle_reference": "env://BACKEND_CA_BUNDLE"}}))
    env["cfg"]["ansible"] = {"playbook_dir": str(tmp_path / "ansible"), "inputs_path": str(inputs), "ssh_key_path": "/run/secrets/cloud_vm_key", "ansible_bin": str(fake)}
    monkeypatch.setenv("SUPABASE_APP_URL", "postgresql://leak-me")
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "do-must-not-pass")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-pass")
    return {"dir": bindir, "mode": bindir / "mode", "argv": bindir / "argv", "env": bindir / "env"}


def bound_real_instance(db, sim):
    """A reserved slot bound to provider resource RID, marked real (not simulated) as a Terraform-created VM would be."""
    op, gen, key, sgen = reserved(db, sim)
    ok, _ = make(BindSlot, sim).run(op, gen, key, sgen, RID, "203.0.113.9", 1, 1024)
    assert ok
    db.execute("UPDATE ops.instances SET simulated = false WHERE provider_resource_id = %s", (RID,))
    instance_id = db.execute("SELECT id::text FROM ops.instances WHERE provider_resource_id = %s", (RID,)).fetchone()[0]
    return op, gen, instance_id


@pytest.mark.parametrize("task", ["ansible_deploy", "deploy"])
def test_ansible_deploy_runs_the_playbook_for_one_instance_and_returns_the_verified_facts(db, sim, env, ans, task):
    op, gen, iid = bound_real_instance(db, sim)
    ok, res = real_heavy(sim, env).run(task, "heavy", op, gen, "digitalocean", RID)
    assert ok and res["result"] == "ok" and res["state"] == "ready" and res["identity"] == iid and res["image_digest"] == DIGEST
    assert res["simulated"] is False and res["task"] == task
    argv = ans["argv"].read_text().split()
    assert argv[argv.index("--limit") + 1] == iid and argv[-1].endswith("playbooks/app.yml") and argv.count("-e") == 1
    assert "postgresql://leak-me" not in " ".join(argv)


def test_ansible_deploy_environment_is_scrubbed_and_passes_only_the_named_secrets(db, sim, env, ans):
    op, gen, iid = bound_real_instance(db, sim)
    real_heavy(sim, env).run("ansible_deploy", "heavy", op, gen, "digitalocean", RID)
    names = ans["env"].read_text().split()
    assert "SUPABASE_APP_URL" in names and "DR_AUTOMATION_URL" in names and "CLOUD_VM_SSH_KEY_FILE" in names
    assert "ANSIBLE_STDOUT_CALLBACK" in names and "ANSIBLE_SHOW_CUSTOM_STATS" in names
    assert "DIGITALOCEAN_TOKEN" not in names  # provider API tokens are Terraform's, not Ansible's
    assert apply_args(env) == []  # no Terraform


def test_ansible_deploy_failure_is_a_failed_attempt_not_ambiguous(db, sim, env, ans):
    op, gen, iid = bound_real_instance(db, sim)
    ans["mode"].write_text("fail\n")
    ok, res = real_heavy(sim, env).run("ansible_deploy", "heavy", op, gen, "digitalocean", RID)
    assert ok and res["result"] == "failed" and res["code"] == "deploy_failed"
    assert res["result"] != "timeout_ambiguous"


@pytest.mark.parametrize("mode", ["no_stats", "wrong_identity"])
def test_ansible_deploy_success_without_verified_facts_is_failed(db, sim, env, ans, mode):
    op, gen, iid = bound_real_instance(db, sim)
    ans["mode"].write_text(mode + "\n")
    ok, res = real_heavy(sim, env).run("ansible_deploy", "heavy", op, gen, "digitalocean", RID)
    assert ok and res["result"] == "failed" and res["code"] == "deploy_unverified"


def test_ansible_deploy_timeout_is_a_failed_attempt(db, sim, env, ans, monkeypatch):
    import run_heavy as rh

    op, gen, iid = bound_real_instance(db, sim)
    ans["mode"].write_text("sleep=5\n")
    monkeypatch.setattr(rh, "ACTION_TIMEOUT_S", 62)  # subprocess budget = 62 - 60 margin = 2 s
    ok, res = real_heavy(sim, env).run("ansible_deploy", "heavy", op, gen, "digitalocean", RID)
    assert ok and res["result"] == "failed" and res["code"] == "deploy_timeout"


def test_ansible_deploy_asserts_the_lease_and_an_unknown_instance_is_failed(db, sim, env, ans):
    op, gen, iid = bound_real_instance(db, sim)
    assert real_heavy(sim, env).run("ansible_deploy", "heavy", op, gen + 1, "digitalocean", RID) == (False, "lease_lost")
    assert not ans["argv"].exists()
    ok, res = real_heavy(sim, env).run("ansible_deploy", "heavy", op, gen, "digitalocean", "999999")
    assert ok and res["result"] == "failed" and res["code"] == "instance_not_found" and not ans["argv"].exists()
    assert real_heavy(sim, env).run("ansible_deploy", "heavy", op, gen, "digitalocean") == (False, "bad_argument")


def test_ansible_deploy_without_its_settings_is_a_config_error(db, sim, env):
    op, gen, iid = bound_real_instance(db, sim)
    assert real_heavy(sim, env).run("ansible_deploy", "heavy", op, gen, "digitalocean", RID) == (False, "provider_adapter_missing_or_unknown")


# ------------------------------------------------------------------ configuration
def schema_validator():
    with open(os.path.join(HERE, "..", "config.schema.yaml")) as f:
        props = yaml.safe_load(f)
    return Draft3Validator({"type": "object", "properties": props, "additionalProperties": False})


def full_config(env):
    return {"supabase_ops_url": "postgresql://x", "provider_adapter": "real", "policy_compile": "simulation", "real_adapter": copy.deepcopy(env["cfg"])}


def test_the_schema_accepts_a_complete_real_configuration(env):
    cfg = full_config(env)
    assert list(schema_validator().iter_errors(cfg)) == []


REQUIRED_PATHS = [("tf_slot_script",), ("tf_state_dir",)] + [(p, k) for p in ("digitalocean", "linode") for k in ("token_env", "region", "size", "image", "ssh_key_id_env")] + [("digitalocean",), ("linode",)]


@pytest.mark.parametrize("path", REQUIRED_PATHS, ids=["/".join(p) for p in REQUIRED_PATHS])
def test_the_schema_rejects_a_real_adapter_object_that_misses_any_required_key(env, path):
    cfg = full_config(env)
    node = cfg["real_adapter"]
    for k in path[:-1]:
        node = node[k]
    del node[path[-1]]
    assert list(schema_validator().iter_errors(cfg)), f"missing {path} was accepted"


def test_the_schema_rejects_unknown_keys_in_the_real_adapter_object(env):
    cfg = full_config(env)
    cfg["real_adapter"]["digitalocean"]["token"] = "literal-secret"
    assert list(schema_validator().iter_errors(cfg))


@pytest.mark.parametrize("path", REQUIRED_PATHS, ids=["/".join(p) for p in REQUIRED_PATHS])
def test_the_adapter_itself_refuses_a_configuration_missing_any_required_key(env, path):
    cfg = copy.deepcopy(env["cfg"])
    node = cfg
    for k in path[:-1]:
        node = node[k]
    del node[path[-1]]
    with pytest.raises(AdapterConfigError, match="real_adapter_config_missing"):
        RealAdapter(cfg)


def test_adapter_selection_is_unchanged_and_real_needs_its_settings(env, tmp_path):
    with pytest.raises(AdapterConfigError, match="real_adapter_config_missing"):
        providers.load({"provider_adapter": "real"})
    assert providers.load({"provider_adapter": "real", "real_adapter": env["cfg"]}).mode == "real"
    assert providers.load({"provider_adapter": "simulation", "simulation_state_dir": str(tmp_path / "s")}).simulated is True
    for bad in ({}, {"provider_adapter": "aws"}, None):
        with pytest.raises(AdapterConfigError, match="provider_adapter_missing_or_unknown"):
            providers.load(bad)
