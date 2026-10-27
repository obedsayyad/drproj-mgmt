"""The local simulation adapter and the adapter switch. No database, no st2, no cloud."""
import json
import os
import stat
import threading

import pytest

import providers
from providers import AdapterConfigError, tags_for
from providers.real import RealAdapter
from real_cfg import REAL_CFG
from providers.simulation import SimulationAdapter

OP = "11111111-1111-4111-8111-111111111111"


def adapter(tmp_path, faults=None):
    path = None
    if faults is not None:
        path = str(tmp_path / "faults.json")
        with open(path, "w") as f:
            json.dump(faults, f)
    return SimulationAdapter(str(tmp_path / "sim"), path)


def create(a, provider="digitalocean", slot="elastic-do-1", attempt=1, op=OP):
    return a.create(provider, slot, op, attempt, tags_for(slot, op, attempt), {})


def test_create_get_list_delete_round_trip_with_exact_tag_scope(tmp_path):
    a = adapter(tmp_path)
    r = create(a)
    assert r["result"] == "ok" and r["resource_id"].startswith("sim-digitalocean-")
    assert a.get("digitalocean", r["resource_id"])["state"] == "present"
    assert a.get("linode", r["resource_id"])["state"] == "absent"  # another provider's namespace
    other = create(a, slot="elastic-do-2", attempt=2)
    mine = a.list_by_tag("digitalocean", tags_for("elastic-do-1", OP, 1))
    assert [x["resource_id"] for x in mine["resources"]] == [r["resource_id"]]
    assert {x["resource_id"] for x in a.list_by_tag("digitalocean", {"project": "cdr"})["resources"]} == {r["resource_id"], other["resource_id"]}
    assert a.delete("digitalocean", r["resource_id"]) == {"result": "ok"}
    assert a.get("digitalocean", r["resource_id"])["state"] == "absent"
    assert a.list_by_tag("digitalocean", tags_for("elastic-do-1", OP, 1))["resources"] == []


def test_no_faults_by_default(tmp_path):
    assert adapter(tmp_path).faults("digitalocean") == {}
    assert adapter(tmp_path, {}).observe("linode") == "available"
    empty = tmp_path / "empty.json"
    empty.write_text("")
    assert SimulationAdapter(str(tmp_path / "s2"), str(empty)).faults("linode") == {}  # unreadable file = no faults


def test_create_timeout_creates_the_resource_but_reports_ambiguous(tmp_path):
    a = adapter(tmp_path, {"digitalocean": {"create_timeout": True}})
    r = create(a)
    assert r == {"result": "timeout_ambiguous", "resource_id": None}
    assert len(a.list_by_tag("digitalocean", tags_for("elastic-do-1", OP, 1))["resources"]) == 1  # it exists
    assert len(a.calls("create")) == 1


def test_api_unknown_answers_unknown_and_deletes_nothing(tmp_path):
    a = adapter(tmp_path)
    rid = create(a)["resource_id"]
    (tmp_path / "faults.json").write_text(json.dumps({"digitalocean": {"api_unknown": True}}))
    f = SimulationAdapter(str(tmp_path / "sim"), str(tmp_path / "faults.json"))  # same state dir
    assert f.get("digitalocean", rid)["state"] == "unknown"
    assert f.list_by_tag("digitalocean", {"project": "cdr"})["state"] == "unknown"
    assert f.delete("digitalocean", rid) == {"result": "unknown"}
    assert f.check_ready("digitalocean", rid) == {"state": "unknown"}
    assert SimulationAdapter(str(tmp_path / "sim"), None).get("digitalocean", rid)["state"] == "present"


def test_unavailable_provider_refuses_create_and_is_observed_unavailable(tmp_path):
    a = adapter(tmp_path, {"linode": {"unavailable": True}})
    assert a.observe("linode") == "unavailable" and a.observe("digitalocean") == "available"
    assert create(a, provider="linode", slot="elastic-linode-1") == {"result": "unavailable"}
    assert SimulationAdapter(str(tmp_path / "sim"), None).list_by_tag("linode", {"project": "cdr"})["resources"] == []


def test_readiness_needs_deploy_then_the_delay_and_never_ready_never_passes(tmp_path):
    a = adapter(tmp_path, {"digitalocean": {"ready_after_s": 0}, "linode": {"never_ready": True}})
    rid = create(a)["resource_id"]
    assert a.check_ready("digitalocean", rid)["state"] == "not_ready"  # not deployed yet
    assert a.deploy("digitalocean", rid) == {"result": "ok"}
    ready = a.check_ready("digitalocean", rid)
    assert ready["state"] == "ready" and ready["image_digest"].startswith("sha256:") and ready["identity"] == rid
    lid = create(a, provider="linode", slot="elastic-linode-1")["resource_id"]
    a.deploy("linode", lid)
    assert a.check_ready("linode", lid)["state"] == "not_ready"
    slow = adapter(tmp_path, {"digitalocean": {"ready_after_s": 3600}})
    assert slow.check_ready("digitalocean", rid)["state"] == "not_ready"


def test_concurrent_creates_get_unique_ids_and_every_call_is_logged(tmp_path):
    a = adapter(tmp_path)
    ids, errors = [], []

    def work(n):
        try:
            ids.append(create(a, slot=f"elastic-do-{n % 5 + 1}", attempt=n)["resource_id"])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(set(ids)) == 16
    assert len(a.calls("create")) == 16
    assert len(a.list_by_tag("digitalocean", {"project": "cdr"})["resources"]) == 16


def test_state_directory_is_private(tmp_path):
    adapter(tmp_path)
    assert stat.S_IMODE(os.stat(tmp_path / "sim").st_mode) == 0o700


def test_metrics_fresh_flag_defaults_true_and_can_be_cleared(tmp_path):
    a = adapter(tmp_path)
    assert a.metrics_fresh() is True
    a.set_metrics_fresh(False)
    assert a.metrics_fresh() is False


def test_provider_adapter_has_no_default(tmp_path):
    for cfg in ({}, {"provider_adapter": None}, {"provider_adapter": "mock"}, None):
        with pytest.raises(AdapterConfigError):
            providers.load(cfg)


def test_load_selects_simulation_or_real_and_the_real_adapter_leaves_deploy_readiness_routing_and_drain_unimplemented(tmp_path):
    sim = providers.load({"provider_adapter": "simulation", "simulation_state_dir": str(tmp_path / "s")})
    assert sim.simulated is True and sim.mode == "simulation" and sim.faults("digitalocean") == {}
    real = providers.load({"provider_adapter": "real", "real_adapter": REAL_CFG})
    assert isinstance(real, RealAdapter) and real.simulated is False and real.mode == "real"
    for call in (lambda: real.deploy("linode", "x"), lambda: real.check_ready("linode", "x"), lambda: real.set_routing("warm", "active"),
                 lambda: real.drain("linode", "x"), lambda: real.pinned_digest()):
        with pytest.raises(NotImplementedError):
            call()
