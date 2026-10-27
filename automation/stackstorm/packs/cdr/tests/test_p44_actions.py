"""P4.4 actions against a REAL migrated Postgres (TEST_DATABASE_URL, needs ops/0013) and the real simulation adapter on a temporary directory;
only the StackStorm API is a fake (a test double, not StackStorm). Same login-role setup as test_capacity_actions.py."""
import copy
import http.server
import json
import os
from real_cfg import REAL_CFG
import shutil
import threading

import pytest

from alert_intent import AlertIntent  # noqa: F401 - keeps the import order of test_p43_actions
from fail_operation import FailOperation
from not_implemented import NotImplementedStep
from plan_next_attempt import PlanNextAttempt
from policy_activate import PolicyActivate
from policy_compile import PolicyCompile
from policy_rollback import PolicyRollback
from policy_validate import PolicyValidate
from policy_verify import PolicyVerify
from provider_power_on import ProviderPowerOn
from reconcile_pass import ReconcilePass
from reserve_slot import ReserveSlot
from standby_classify import StandbyClassify
from standby_unhealthy import StandbyUnhealthy
from standby_verify import StandbyVerify
from standby_window import StandbyWindow
from test_capacity_actions import ADMIN, db, login_url  # noqa: F401 - db is a fixture
from test_p43_actions import FakeApi, lease_of, make, new_op, provisioned, slot
from providers.simulation import SimulationAdapter
import prom_render

pytestmark = pytest.mark.skipif(not ADMIN, reason="needs TEST_DATABASE_URL (a database migrated through ops/0013)")

HERE = os.path.dirname(__file__)
PROM_FILE = os.path.join(HERE, "..", "..", "..", "..", "..", "deploy", "compose", "mgmt", "prometheus", "prometheus.yml")
DEFAULTS = json.load(open(os.path.join(HERE, "..", "..", "..", "..", "..", "docs", "config", "policy-defaults.json")))


@pytest.fixture()
def sim(tmp_path):
    return SimulationAdapter(str(tmp_path))


def faults(tmp_path, value):
    path = tmp_path / "faults.json"
    path.write_text(json.dumps(value))
    return str(path)


def deployed_warm(db, sim, tmp_path, provider="digitalocean", kind="warm"):
    """A bound, ready, DEPLOYED warm-role standby (the simulation reports it ready at once)."""
    key, rid, op = provisioned(db, sim, tmp_path, provider, kind)
    assert sim.deploy(provider, rid)["result"] == "ok"
    return key, rid


def classify(db, tmp_path, **cfg):
    op, gen = lease_of(db, "standby_repair")
    ok, out = make(StandbyClassify, tmp_path, **cfg).run(op, gen)
    assert ok, out
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (op, gen))
    db.execute("UPDATE ops.operations SET status = 'succeeded' WHERE id = %s::uuid", (op,))
    return out


READY_NOW = {"*": {"ready_after_s": 0}}


# ------------------------------------------------------------------ standby_classify (docs/04 repair step 3)
def test_classify_healthy_app_broken_powered_off_and_absent_follow_the_provider_answers(db, sim, tmp_path):
    _key, rid = deployed_warm(db, sim, tmp_path)
    f = faults(tmp_path, READY_NOW)
    out = classify(db, tmp_path, simulation_faults=f)
    assert out["class"] == "healthy" and out["slot_key"] == "warm" and out["resource_id"] == rid and out["image_digest"] == sim.pinned_digest()
    sim.set_resource(rid, app_broken=True)
    assert classify(db, tmp_path, simulation_faults=f)["class"] == "app_broken"
    sim.set_resource(rid, app_broken=False, power="off")
    assert classify(db, tmp_path, simulation_faults=f)["class"] == "powered_off"
    sim.set_resource(rid, power="on", deleted=True)
    out = classify(db, tmp_path, simulation_faults=f)
    assert out["class"] == "absent" and out["slot_state"] == "bound"
    assert out["evidence"]["confirmed_absent"] is True and out["evidence"]["source"] == "provider_api" and out["evidence"]["simulated"] is True


@pytest.mark.parametrize("fault", [{"api_unknown": True}, {"unavailable": True}])
def test_classify_unknown_when_the_provider_does_not_answer_and_never_reports_absent(db, sim, tmp_path, fault):
    deployed_warm(db, sim, tmp_path)
    out = classify(db, tmp_path, simulation_faults=faults(tmp_path, {"digitalocean": fault}))
    assert out["class"] == "unknown" and out["reason"] == "provider_answer_unknown" and "evidence" not in out
    assert out["other_provider"] == "linode" and out["other_available"] is True


def test_classify_unknown_when_a_tagged_resource_nothing_explains_claims_the_slot(db, sim, tmp_path):
    _key, rid = deployed_warm(db, sim, tmp_path)
    sim.inject("digitalocean", {"project": "cdr", "slot": "warm", "op": "x", "attempt": "9"})
    sim.set_resource(rid, deleted=True)  # even the recorded VM being gone must not read as "absent" while a stranger holds the tag
    out = classify(db, tmp_path, simulation_faults=faults(tmp_path, READY_NOW))
    assert out["class"] == "unknown" and out["reason"] == "unexpected_tagged_resource"


def test_classify_slot_states_free_held_busy_and_maintenance(db, sim, tmp_path):
    out = classify(db, tmp_path)  # a free warm slot: nothing to release, reserve and provision
    assert out["class"] == "absent" and out["evidence"] is None and out["provider"] == "digitalocean" and out["slot_state"] == "free"
    assert classify(db, tmp_path, simulation_faults=faults(tmp_path, {"digitalocean": {"unavailable": True}}))["class"] == "unknown"
    deployed_warm(db, sim, tmp_path)
    db.execute("UPDATE ops.capacity_slots SET state = 'held_uncertain' WHERE key = 'warm'")
    out = classify(db, tmp_path)
    assert out["class"] == "held_uncertain" and out["interim_bound"] is False and out["other_available"] is True
    db.execute("UPDATE ops.capacity_slots SET state = 'releasing' WHERE key = 'warm'")
    assert classify(db, tmp_path)["class"] == "busy"
    db.execute("UPDATE ops.capacity_slots SET state = 'bound' WHERE key = 'warm'")
    db.execute("UPDATE ops.runtime_flags SET maintenance = true")
    assert classify(db, tmp_path)["class"] == "maintenance"
    db.execute("UPDATE ops.runtime_flags SET maintenance = false")


def test_classify_reports_an_existing_interim_warm(db, sim, tmp_path):
    deployed_warm(db, sim, tmp_path)
    db.execute("UPDATE ops.capacity_slots SET state = 'held_uncertain' WHERE key = 'warm'")
    key, _ = provisioned(db, sim, tmp_path, "linode", "interim_warm")[:2]
    assert key == "interim-warm-linode"
    assert classify(db, tmp_path)["interim_bound"] is True


# ------------------------------------------------------------------ power on / window / verify
def test_power_on_is_fenced_and_turns_the_resource_on(db, sim, tmp_path):
    _key, rid = deployed_warm(db, sim, tmp_path)
    sim.set_resource(rid, power="off")
    op, gen = lease_of(db, "standby_repair")
    ok, out = make(ProviderPowerOn, tmp_path).run(op, gen + 1, "digitalocean", rid)  # a fenced (stale) generation never reaches the provider
    assert (ok, out) == (False, "lease_lost") and sim.calls("power_on") == []
    ok, out = make(ProviderPowerOn, tmp_path).run(op, gen, "digitalocean", rid)
    assert ok and out["result"] == "ok" and sim.get("digitalocean", rid)["power"] == "on" and len(sim.calls("power_on")) == 1


def test_power_on_unknown_answer_is_a_result_not_success(db, sim, tmp_path):
    _key, rid = deployed_warm(db, sim, tmp_path)
    op, gen = lease_of(db, "standby_repair")
    ok, out = make(ProviderPowerOn, tmp_path, simulation_faults=faults(tmp_path, {"digitalocean": {"api_unknown": True}})).run(op, gen, "digitalocean", rid)
    assert ok and out["result"] == "unknown"


def test_standby_window_uses_the_captured_policy(db, tmp_path):
    op = new_op(db, "standby_repair")
    ok, out = make(StandbyWindow, tmp_path).run(op)
    assert ok and len(out["checkpoints_at"]) == 3 and out["deadline_at"] == out["checkpoints_at"][-1]


def test_verify_needs_ready_digest_and_backup_routing(db, sim, tmp_path):
    key, rid = deployed_warm(db, sim, tmp_path)
    db.execute("UPDATE ops.instances SET image_digest = %s WHERE slot_key = 'warm'", (sim.pinned_digest(),))
    cfg = {"simulation_faults": faults(tmp_path, READY_NOW)}
    run = lambda: make(StandbyVerify, tmp_path, **cfg).run(new_op(db, "standby_repair"), 0, key, "digitalocean", rid)[1]
    out = run()
    assert out["verified"] is False and out["checks"]["routing_backup"] is False and out["checks"]["ready"] and out["checks"]["digest_matches"]
    db.execute("UPDATE ops.instances SET routing_mode = 'backup' WHERE slot_key = 'warm'")
    assert run()["verified"] is False  # the simulated HAProxy view still says none
    sim.set_routing("warm", "backup")
    assert run()["verified"] is True
    db.execute("UPDATE ops.instances SET image_digest = 'sha256:old' WHERE slot_key = 'warm'")
    out = run()
    assert out["verified"] is False and out["checks"]["digest_matches"] is False
    sim.set_resource(rid, app_broken=True)
    assert run()["checks"]["ready"] is False


# ------------------------------------------------------------------ simulation producer of standby.unhealthy
class RunApi:
    def __init__(self):
        self.runs = []

    def run(self, ref, params):
        self.runs.append((ref, params))
        return "exec-1"


def producer(tmp_path, api, **cfg):
    a = make(StandbyUnhealthy, tmp_path, **cfg)
    a._api = lambda: api
    return a


def test_producer_breaks_the_standby_in_the_simulation_opens_one_operation_and_dispatches_repair(db, sim, tmp_path):
    _key, rid = deployed_warm(db, sim, tmp_path)
    api = RunApi()
    ok, out = producer(tmp_path, api).run("powered_off")
    assert ok and out["simulated"] is True and out["created"] is True and api.runs == [("cdr.standby_repair", {"operation_id": out["operation_id"]})]
    assert sim.get("digitalocean", rid)["power"] == "off"
    assert db.execute("SELECT kind, status FROM ops.operations WHERE id = %s::uuid", (out["operation_id"],)).fetchone() == ("standby_repair", "queued")
    assert db.execute("SELECT count(*) FROM ops.operation_events WHERE operation_id = %s::uuid AND event_type = 'standby.unhealthy'", (out["operation_id"],)).fetchone()[0] == 1


@pytest.mark.parametrize("fault,field", [("app_broken", "app_broken"), ("absent", "deleted")])
def test_producer_faults(db, sim, tmp_path, fault, field):
    _key, rid = deployed_warm(db, sim, tmp_path)
    assert producer(tmp_path, RunApi()).run(fault)[0]
    assert json.load(open(tmp_path / "state.json"))["resources"][rid][field] is True


def test_producer_refuses_the_real_adapter_and_a_missing_resource(db, sim, tmp_path):
    api = RunApi()
    assert producer(tmp_path, api, provider_adapter="real", real_adapter=REAL_CFG).run("none") == (False, "not_simulation")
    assert producer(tmp_path, api).run("app_broken") == (False, "no_warm_resource_to_break")  # nothing is bound: nothing to break, nothing opened
    assert api.runs == [] and db.execute("SELECT count(*) FROM ops.operations WHERE kind = 'standby_repair'").fetchone()[0] == 0


# ------------------------------------------------------------------ plan_next_attempt for a standby slot
def test_standby_attempts_retry_only_the_same_provider_then_exhaust(db, sim, tmp_path):
    op, gen = lease_of(db, "standby_repair")
    run = lambda attempts, kind="warm", provider="digitalocean": make(PlanNextAttempt, tmp_path).run(op, gen, provider, attempts, kind)[1]
    out = run({"digitalocean": 1})
    assert out["plan"] == "retry_same" and out["provider"] == "digitalocean" and out["slot_key"] == "warm"
    db.execute("UPDATE ops.capacity_slots SET state = 'free', bound_instance_id = NULL, lease_operation_id = NULL")
    db.execute("DELETE FROM ops.instances")
    assert run({"digitalocean": 2})["plan"] == "exhausted"  # never alternates to Linode for the warm slot
    assert slot(db, "warm")[0] == "free" and db.execute("SELECT count(*) FROM ops.capacity_slots WHERE state <> 'free'").fetchone()[0] == 0


def test_elastic_attempts_still_alternate(db, sim, tmp_path):
    op, gen = lease_of(db, "scale_out")
    out = make(PlanNextAttempt, tmp_path).run(op, gen, "linode", {"linode": 2})[1]
    assert out["plan"] == "alternate" and out["provider"] == "digitalocean"


# ------------------------------------------------------------------ interim warm: counted, capped (T36 data rules through the action)
def test_interim_warm_counts_against_the_other_providers_cap_and_blocks_reservation_beyond_it(db, sim, tmp_path):
    deployed_warm(db, sim, tmp_path)
    db.execute("UPDATE ops.capacity_slots SET state = 'held_uncertain' WHERE key = 'warm'")
    op, gen = lease_of(db, "standby_repair")
    ok, r = make(ReserveSlot, tmp_path).run(op, gen, "linode", "interim_warm", True)
    assert ok and r["slot_key"] == "interim-warm-linode"
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (op, gen))
    assert db.execute("SELECT provider, count(*) FROM ops.capacity_slots WHERE state <> 'free' GROUP BY provider ORDER BY provider").fetchall() == [("digitalocean", 1), ("linode", 1)]
    db.execute("UPDATE ops.policy_versions SET configuration = jsonb_set(configuration, '{capacity,max_cloud_vms_per_provider,linode}', '1') WHERE status = 'active'")
    op2, gen2 = lease_of(db, "scale_out")
    assert make(ReserveSlot, tmp_path).run(op2, gen2, "linode", "elastic", True) == (True, {"reserved": False})  # the interim slot fills Linode's cap of 1


# ------------------------------------------------------------------ apply_policy actions
def seed_prom(tmp_path, version="1"):
    d = tmp_path / "prometheus"
    d.mkdir(exist_ok=True)
    text = prom_render.set_policy_version(open(PROM_FILE).read(), version)
    (d / "prometheus.yml").write_text(text)
    (d / "prometheus.loaded.yml").write_text(text)


def merge(base, changes):
    out = copy.deepcopy(base)
    for k, v in changes.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def pending(db, changes, version=2):
    db.execute("INSERT INTO ops.policy_versions (version, configuration, status, activation_phase) VALUES (%s, %s, 'pending', 'validating')", (version, json.dumps(merge(DEFAULTS, changes))))
    return db.execute("INSERT INTO ops.operations (kind, policy_version, status, parameters) VALUES ('apply_policy', 1, 'running', %s) RETURNING id::text",
                      (json.dumps({"policy_version": version}),)).fetchone()[0]


def pol(cls, tmp_path, **cfg):
    return make(cls, tmp_path, policy_compile="simulation", **cfg)


def statuses(db):
    return db.execute("SELECT version, status, activation_phase FROM ops.policy_versions ORDER BY version").fetchall()


def test_validate_reports_what_the_change_touches(db, tmp_path):
    ok, out = pol(PolicyValidate, tmp_path).run(pending(db, {"scale_out": {"cooldown_seconds": 90}}))
    assert ok and out["ok"] and out["changed_paths"] == ["scale_out.cooldown_seconds"] and out["capacity_changed"] is False and out["has_unimplemented"] is False


def test_validate_flags_capacity_and_every_not_implemented_target(db, tmp_path):
    op = pending(db, {"capacity": {"max_cloud_vms_per_provider": {"linode": 3}}, "health": {"interval_seconds": 5}, "demo": {"max_rate_rps": 100},
                      "scale_out": {"cpu_percent": 70}, "backup": {"cron": "0 1 * * *"}})
    ok, out = pol(PolicyValidate, tmp_path).run(op)
    assert ok and out["ok"] and out["capacity_changed"] is True and out["has_unimplemented"] is True
    assert out["unimplemented"] == {"prometheus_rules": ["scale_out.cpu_percent"], "haproxy": ["health.interval_seconds"], "k6": ["demo.max_rate_rps"], "stackstorm_timers": ["backup.cron"]}


def test_validate_rejects_bounds_and_a_version_that_is_not_pending(db, tmp_path):
    ok, out = pol(PolicyValidate, tmp_path).run(pending(db, {"capacity": {"max_cloud_vms_per_provider": {"digitalocean": 0}}}))
    assert ok and out["ok"] is False and "warm provider needs a cap of at least 1" in out["errors"][0]
    db.execute("UPDATE ops.policy_versions SET status = 'failed' WHERE version = 2")
    ok, out = pol(PolicyValidate, tmp_path).run(db.execute("SELECT id::text FROM ops.operations WHERE kind = 'apply_policy'").fetchone()[0])
    assert ok and out["ok"] is False and "not pending" in out["errors"][0]
    assert pol(PolicyValidate, tmp_path).run(new_op(db, "apply_policy")) == (False, "not_an_apply_policy_operation")  # no policy_version parameter


def test_validate_rejects_a_no_change(db, tmp_path):
    ok, out = pol(PolicyValidate, tmp_path).run(pending(db, {}))
    assert ok and out["ok"] is False and "does not change" in out["errors"][0]


def test_compile_verify_activate_rerenders_the_label_and_activates(db, tmp_path):
    seed_prom(tmp_path)
    op = pending(db, {"scale_out": {"cooldown_seconds": 90}})
    ok, out = pol(PolicyCompile, tmp_path).run(op)
    assert ok and out["label"] == "2" and out["previous_label"] == "1" and out["mode"] == "simulation"
    assert prom_render.read_policy_version((tmp_path / "prometheus" / "prometheus.yml").read_text()) == "2"
    assert db.execute("SELECT activation_phase FROM ops.policy_versions WHERE version = 2").fetchone()[0] == "applying"
    assert pol(PolicyVerify, tmp_path).run(op) == (True, {"loaded_label": "2"})
    assert db.execute("SELECT activation_phase FROM ops.policy_versions WHERE version = 2").fetchone()[0] == "verifying"
    ok, out = pol(PolicyActivate, tmp_path).run(op)
    assert ok and out["state"] == "active" and out["pending_transition"] == []
    assert statuses(db) == [(1, "superseded", "completed"), (2, "active", "completed")]
    assert [r["method"] for r in SimulationAdapter(str(tmp_path)).calls()] == ["prometheus_reload"]


def test_failed_reload_keeps_the_old_version_and_rollback_restores_the_label(db, tmp_path):
    seed_prom(tmp_path)
    op = pending(db, {"scale_out": {"cooldown_seconds": 90}})
    cfg = {"simulation_faults": faults(tmp_path, {"*": {"prometheus_reload_fail": True}})}
    assert pol(PolicyCompile, tmp_path, **cfg).run(op) == (False, "reload_failed")
    assert pol(PolicyVerify, tmp_path, **cfg).run(op) == (False, "verify_failed")  # what Prometheus LOADED is still version 1
    (tmp_path / "faults.json").write_text("{}")
    ok, out = pol(PolicyRollback, tmp_path).run(op, str({"stdout": "", "stderr": "", "exit_code": 0, "result": "reload_failed"}))
    assert ok and out == {"label_restored": True, "prometheus_touched": True, "version_marked_failed": True, "active_version": 1}
    assert prom_render.read_policy_version((tmp_path / "prometheus" / "prometheus.yml").read_text()) == "1"
    assert statuses(db) == [(1, "active", "completed"), (2, "failed", "verifying")]
    assert db.execute("SELECT apply_result FROM ops.policy_versions WHERE version = 2").fetchone()[0]["error"] == "reload_failed"
    assert pol(PolicyRollback, tmp_path).run(op, "again")[1]["version_marked_failed"] is False  # idempotent


def test_rollback_before_anything_was_applied_does_not_touch_prometheus_and_records_a_code(db, tmp_path):
    seed_prom(tmp_path)
    op = pending(db, {"health": {"interval_seconds": 5}})
    ok, out = pol(PolicyRollback, tmp_path).run(op, str({"code": "not_implemented", "detail": {"haproxy": ["health.interval_seconds"]}}))
    assert ok and out["prometheus_touched"] is False and out["version_marked_failed"] is True
    assert SimulationAdapter(str(tmp_path)).calls("prometheus_reload") == []
    assert db.execute("SELECT apply_result FROM ops.policy_versions WHERE version = 2").fetchone()[0]["error"] == "not_implemented"


def test_compile_without_the_prometheus_file_is_a_named_failure_not_an_empty_success(db, tmp_path):
    assert pol(PolicyCompile, tmp_path).run(pending(db, {"scale_out": {"cooldown_seconds": 90}})) == (False, "template_missing")


def test_policy_compile_setting_has_no_default(db, tmp_path):
    op = pending(db, {"scale_out": {"cooldown_seconds": 90}})
    assert make(PolicyCompile, tmp_path).run(op) == (False, "policy_compile_missing_or_unknown")
    assert make(PolicyCompile, tmp_path, policy_compile="real").run(op) == (False, "policy_compile_real_needs_path_and_url")


def test_capacity_lowered_below_occupied_is_a_pending_transition_that_deletes_nothing(db, sim, tmp_path):
    for _ in range(3):
        provisioned(db, sim, tmp_path, "linode")
    pending(db, {"capacity": {"max_cloud_vms_per_provider": {"linode": 2}}})
    seed_prom(tmp_path)
    run = lease_of(db, "apply_policy")  # the capacity change runs under the lease
    db.execute("UPDATE ops.operations SET parameters = %s WHERE id = %s::uuid", (json.dumps({"policy_version": 2}), run[0]))
    ok, out = pol(PolicyActivate, tmp_path).run(run[0], run[1])
    assert ok and out["state"] == "active" and out["pending_transition"] == [{"provider": "linode", "occupied": 3, "cap": 2}]
    assert db.execute("SELECT count(*) FROM ops.capacity_slots WHERE provider = 'linode' AND state = 'bound'").fetchone()[0] == 3  # nothing released
    assert sim.calls("delete") == []
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (run[0], run[1]))
    op2, gen2 = lease_of(db, "scale_out")
    assert make(ReserveSlot, tmp_path).run(op2, gen2, "linode", "elastic", True) == (True, {"reserved": False})  # no new slot above the new cap


def test_activation_under_a_stale_lease_generation_is_fenced(db, tmp_path):
    seed_prom(tmp_path)
    run, gen = lease_of(db, "apply_policy")
    pending(db, {"scale_out": {"cooldown_seconds": 90}})
    db.execute("UPDATE ops.operations SET parameters = %s WHERE id = %s::uuid", (json.dumps({"policy_version": 2}), run))
    assert pol(PolicyActivate, tmp_path).run(run, gen + 1) == (False, "lease_lost")
    assert statuses(db)[1][1] == "pending"


class Stub(http.server.BaseHTTPRequestHandler):
    label = "1"
    reloads = 0

    def do_POST(self):
        Stub.reloads += 1
        Stub.label = self.server.pending_label()
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        body = json.dumps({"data": {"yaml": f'global:\n  external_labels:\n    policy_version: "{Stub.label}"\n'}}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_real_mode_writes_the_live_file_reloads_over_http_and_verifies_what_prometheus_reports(db, tmp_path):
    live = tmp_path / "live.yml"
    shutil.copy(PROM_FILE, live)
    srv = http.server.HTTPServer(("127.0.0.1", 0), Stub)
    srv.pending_label = lambda: prom_render.read_policy_version(live.read_text())  # a reload makes Prometheus read the file
    Stub.label, Stub.reloads = "1", 0
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cfg = {"policy_compile": "real", "prometheus_config_path": str(live), "prometheus_url": f"http://127.0.0.1:{srv.server_port}"}
        op = pending(db, {"scale_out": {"cooldown_seconds": 90}})
        ok, out = make(PolicyCompile, tmp_path, **cfg).run(op)
        assert ok and out["mode"] == "real" and Stub.reloads == 1 and prom_render.read_policy_version(live.read_text()) == "2"
        assert make(PolicyVerify, tmp_path, **cfg).run(op) == (True, {"loaded_label": "2"})
        assert make(PolicyRollback, tmp_path, **cfg).run(op, "x")[1]["label_restored"] is True and Stub.label == "1"
    finally:
        srv.shutdown()


# ------------------------------------------------------------------ failure codes
def test_not_implemented_step_always_fails_with_its_name_and_phase(tmp_path):
    assert NotImplementedStep().run("ramp_10", "P6") == (False, "not_implemented:ramp_10:P6")


@pytest.mark.parametrize("error,code", [
    ({"code": "not_implemented", "detail": {"haproxy": ["health.interval_seconds"]}}, "not_implemented"),
    ({"stderr": "not_implemented:direct_probes:P6"}, "not_implemented"),
    ({"code": "policy_invalid", "detail": ["x"]}, "policy_invalid"),
    ({"stderr": "reload_failed"}, "reload_failed"),
    ({"stderr": "boom"}, "workflow_error"),
])
def test_fail_operation_keeps_a_named_failure_code(db, tmp_path, error, code):
    op = new_op(db, "apply_policy")
    ok, out = make(FailOperation, tmp_path).run(op, 0, error, "step_x")
    assert ok and out["status"] == "failed"
    row = db.execute("SELECT status, outcome FROM ops.operations WHERE id = %s::uuid", (op,)).fetchone()
    assert row[0] == "failed" and row[1]["error"] == code and row[1]["step"] == "step_x"
    if isinstance(error, dict) and "detail" in error:
        assert row[1]["detail"] == error["detail"]


# ------------------------------------------------------------------ reconcile: standby intents, dead apply_policy
def reconcile(db, tmp_path, api=None, **cfg):
    op, gen = lease_of(db, "reconcile")
    a = make(ReconcilePass, tmp_path, **cfg)
    a._api = lambda: api or FakeApi()
    ok, out = a.run(op, gen)
    assert ok, out
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (op, gen))
    db.execute("UPDATE ops.operations SET status = 'succeeded' WHERE id = %s::uuid", (op,))
    return out


def test_reconcile_requests_a_repair_for_a_failed_warm_instance_once_per_backoff_and_generation(db, sim, tmp_path):
    deployed_warm(db, sim, tmp_path)
    db.execute("UPDATE ops.instances SET lifecycle_state = 'failed' WHERE slot_key = 'warm'")
    out = reconcile(db, tmp_path)
    assert [i["kind"] for i in out["intents"]] == ["standby_repair"] and out["intents"][0]["fingerprint"].startswith("reconcile.warm_mismatch:warm:")
    assert out["resumes"] == [{"ref": "cdr.standby_repair", "parameters": {"operation_id": out["intents"][0]["operation_id"]}}]
    assert db.execute("SELECT status FROM ops.operations WHERE id = %s::uuid", (out["intents"][0]["operation_id"],)).fetchone()[0] == "queued"
    again = reconcile(db, tmp_path)
    assert again["intents"] == []  # one in flight
    db.execute("UPDATE ops.operations SET status = 'failed' WHERE kind = 'standby_repair'")
    assert reconcile(db, tmp_path)["intents"] == []  # backoff since the last request
    db.execute("UPDATE ops.operations SET created_at = now() - interval '10 minutes' WHERE kind = 'standby_repair'")
    assert len(reconcile(db, tmp_path)["intents"]) == 1


def test_reconcile_requests_a_repair_for_a_held_warm_slot_only_while_no_interim_warm_exists(db, sim, tmp_path):
    deployed_warm(db, sim, tmp_path)
    db.execute("UPDATE ops.capacity_slots SET state = 'held_uncertain' WHERE key = 'warm'")
    f = faults(tmp_path, {"digitalocean": {"api_unknown": True}})  # the provider still does not answer, so reconcile cannot resolve the slot
    out = reconcile(db, tmp_path, simulation_faults=f)
    assert len(out["intents"]) == 1 and slot(db, "warm")[0] == "held_uncertain"
    db.execute("UPDATE ops.operations SET status = 'succeeded' WHERE kind = 'standby_repair'")
    db.execute("UPDATE ops.operations SET created_at = now() - interval '10 minutes' WHERE kind = 'standby_repair'")
    provisioned(db, sim, tmp_path, "linode", "interim_warm")
    assert reconcile(db, tmp_path, simulation_faults=f)["intents"] == []


def test_reconcile_requests_the_release_of_the_surplus_interim_warm_only_after_the_warm_slot_is_ready_again(db, sim, tmp_path):
    deployed_warm(db, sim, tmp_path)
    db.execute("UPDATE ops.capacity_slots SET state = 'held_uncertain' WHERE key = 'warm'")  # the interim exists because the warm slot was uncertain
    provisioned(db, sim, tmp_path, "linode", "interim_warm")
    db.execute("UPDATE ops.capacity_slots SET state = 'bound' WHERE key = 'warm'")  # ... and reconcile has resolved it since
    out = reconcile(db, tmp_path)
    assert [i["kind"] for i in out["intents"]] == ["scale_in"] and out["intents"][0]["fingerprint"] == "reconcile.surplus_warm:interim-warm-linode"
    db.execute("UPDATE ops.operations SET status = 'succeeded' WHERE kind = 'scale_in'")
    db.execute("DELETE FROM ops.processed_events")
    db.execute("UPDATE ops.instances SET lifecycle_state = 'failed' WHERE slot_key = 'warm'")
    kinds = [i["kind"] for i in reconcile(db, tmp_path)["intents"]]
    assert kinds == ["standby_repair"]  # the warm one is not ready: the interim is not surplus


def test_reconcile_does_nothing_for_a_healthy_single_standby_or_an_empty_warm_slot(db, sim, tmp_path):
    assert reconcile(db, tmp_path)["intents"] == []  # a free warm slot is not a mismatch: the first standby comes from deployment, not from reconcile
    deployed_warm(db, sim, tmp_path)
    assert reconcile(db, tmp_path)["intents"] == []


def test_reconcile_fails_the_pending_policy_of_a_dead_apply_policy_and_restores_the_label(db, sim, tmp_path):
    seed_prom(tmp_path)
    op = pending(db, {"scale_out": {"cooldown_seconds": 90}})
    pol(PolicyCompile, tmp_path).run(op)  # died after compile, before activation: the label already says 2
    db.execute("UPDATE ops.operations SET updated_at = now() - interval '1 hour', created_at = now() - interval '1 hour' WHERE id = %s::uuid", (op,))
    out = reconcile(db, tmp_path, policy_compile="simulation")
    assert op in out["interrupted"] and out["policy_failed"] == [op]
    assert statuses(db)[1][1] == "failed" and statuses(db)[0][1] == "active"
    assert prom_render.read_policy_version((tmp_path / "prometheus" / "prometheus.yml").read_text()) == "1"
    assert db.execute("SELECT status FROM ops.operations WHERE id = %s::uuid", (op,)).fetchone()[0] == "failed"


def test_reconcile_fails_a_stale_queued_apply_policy_instead_of_calling_it_a_noop(db, tmp_path):
    op = pending(db, {"scale_out": {"cooldown_seconds": 90}})
    db.execute("UPDATE ops.operations SET status = 'queued', created_at = now() - interval '1 hour', updated_at = now() - interval '1 hour' WHERE id = %s::uuid", (op,))
    out = reconcile(db, tmp_path)
    assert op in out["stale_queued"] and out["policy_failed"] == [op]
    assert db.execute("SELECT status, outcome FROM ops.operations WHERE id = %s::uuid", (op,)).fetchone() == ("failed", {"error": "stale_intent"})
    assert statuses(db)[1][1] == "failed"
