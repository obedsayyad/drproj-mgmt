"""P4.3 actions against a REAL migrated Postgres (TEST_DATABASE_URL, needs ops/0012) and the real simulation adapter on a
temporary directory; only the StackStorm API is a fake (a test double, not StackStorm). Same login-role setup as
test_capacity_actions.py."""
import json
import os
import re
import tempfile
from real_cfg import REAL_CFG
import uuid

import psycopg
import pytest
import responses

from adopt_attempt import AdoptAttempt
from alert_intent import AlertIntent
from apply_routing import ApplyRouting
from bind_slot import BindSlot
from control_state import ControlState
from delete_slot_resource import DeleteSlotResource
from drain_instance import DrainInstance
from failover_target import FailoverTarget
from providers import tags_for
from providers.simulation import SimulationAdapter
from reconcile_pass import ReconcilePass
from reserve_slot import ReserveSlot
from scale_in_gate import ScaleInGate
from st2_api import St2Unavailable
from supersede_queued import SupersedeQueued
from test_capacity_actions import ADMIN, db, login_url  # noqa: F401 - db is a fixture
from test_real_adapter import env  # noqa: F401 - fixture reuse
from wait_lease import WaitLease

pytestmark = pytest.mark.skipif(not ADMIN, reason="needs TEST_DATABASE_URL (a database migrated through ops/0012)")


class FakeApi:
    """Stands in for the StackStorm API: executions are registered per (action ref, operation id)."""

    def __init__(self):
        self.live = {}
        self.cancelled = []
        self.unavailable = False

    def find_live(self, ref, op):
        if self.unavailable:
            raise St2Unavailable("down")
        return [{"id": f"exec-{op[:8]}", "status": "running"}] if (ref, op) in self.live else []

    def execution(self, eid):
        if self.unavailable:
            raise St2Unavailable("down")
        return None

    def cancel(self, eid):
        self.cancelled.append(eid)


@pytest.fixture()
def sim(tmp_path):
    return SimulationAdapter(str(tmp_path))


def make(cls, tmp_path, **extra):
    a = cls.__new__(cls)
    a.config = {"supabase_ops_url": login_url(), "provider_adapter": "simulation", "simulation_state_dir": str(tmp_path), **extra}
    return a


def new_op(db, kind, status="running", age_s=0):
    return db.execute(
        "INSERT INTO ops.operations (kind, policy_version, status, created_at, updated_at) VALUES (%s, 1, %s, now() - make_interval(secs => %s), now() - make_interval(secs => %s)) RETURNING id::text",
        (kind, status, age_s, age_s),
    ).fetchone()[0]


def lease_of(db, kind):
    op = new_op(db, kind)
    return op, db.execute("SELECT generation FROM ops.acquire_lease(%s::uuid)", (op,)).fetchone()[0]


def provisioned(db, sim, tmp_path, provider, kind="elastic", ready=True):
    """A finished operation that left a bound (ready) slot with a real simulated resource: key, resource id, op."""
    op, gen = lease_of(db, "scale_out")
    ok, r = make(ReserveSlot, tmp_path).run(op, gen, provider, kind, True)
    key, sgen = r["slot_key"], r["slot_generation"]
    db.execute("SELECT * FROM ops.start_attempt(%s::uuid, %s, 1, %s, %s, %s)", (op, gen, provider, key, sgen))
    res = sim.create(provider, key, op, 1, tags_for(key, op, 1), {})
    make(BindSlot, tmp_path).run(op, gen, key, sgen, res["resource_id"], res["ip"], 1, 1024)
    if ready:
        db.execute("SELECT ops.set_instance_state(%s::uuid, %s, %s, %s, 'ready', 'on', 'sha256:0')", (op, gen, key, sgen))
    db.execute("UPDATE ops.operation_attempts SET status = 'ready' WHERE operation_id = %s::uuid", (op,))
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (op, gen))
    db.execute("UPDATE ops.operations SET status = 'succeeded' WHERE id = %s::uuid", (op,))
    return key, res["resource_id"], op


def slot(db, key):
    return db.execute("SELECT state, generation FROM ops.capacity_slots WHERE key = %s", (key,)).fetchone()


# ------------------------------------------------------------------ scale_in_gate
def gate(db, tmp_path):
    op = new_op(db, "scale_in")
    out = make(ScaleInGate, tmp_path).run(op)[1]
    db.execute("UPDATE ops.operations SET status = 'succeeded' WHERE id = %s::uuid", (op,))  # a finished gate op must not look in flight to the next call
    return out


def test_scale_in_gate_proceeds_only_with_a_ready_warm_standby_and_picks_the_newest_elastic(db, sim, tmp_path):
    provisioned(db, sim, tmp_path, "digitalocean", "warm")
    older, _, _ = provisioned(db, sim, tmp_path, "linode")
    newer, rid, _ = provisioned(db, sim, tmp_path, "digitalocean")
    out = gate(db, tmp_path)
    assert out["proceed"] is True and out["slot_key"] == newer and out["resource_id"] == rid and out["slot_key"] != older


def test_scale_in_gate_refusals(db, sim, tmp_path):
    assert gate(db, tmp_path) == {"proceed": False, "noop_reason": "warm_not_ready"}
    provisioned(db, sim, tmp_path, "digitalocean", "warm")
    assert gate(db, tmp_path) == {"proceed": False, "noop_reason": "no_candidate"}
    provisioned(db, sim, tmp_path, "linode")
    assert gate(db, tmp_path)["proceed"] is True
    sim.set_metrics_fresh(False)
    assert gate(db, tmp_path)["noop_reason"] == "metrics_stale"
    sim.set_metrics_fresh(True)
    db.execute("SELECT ops.set_metrics_stale(true)")
    assert gate(db, tmp_path)["noop_reason"] == "metrics_stale"
    db.execute("SELECT ops.set_metrics_stale(false)")
    db.execute("UPDATE ops.runtime_flags SET maintenance = true")
    assert gate(db, tmp_path)["noop_reason"] == "maintenance"
    db.execute("UPDATE ops.runtime_flags SET maintenance = false")


@pytest.mark.parametrize("status", ["running", "verifying", "paused", "reconciling"])
def test_scale_in_gate_noops_while_another_scale_in_is_in_flight_whatever_its_fingerprint(db, sim, tmp_path, status):
    provisioned(db, sim, tmp_path, "digitalocean", "warm")
    provisioned(db, sim, tmp_path, "linode")
    first = new_op(db, "scale_in", status)  # started by an earlier cpu.low; the gate op below is a different one
    assert gate(db, tmp_path) == {"proceed": False, "noop_reason": "scale_in_in_progress"}  # a new op (a different cpu.low fingerprint) coalesces
    db.execute("UPDATE ops.operations SET status = 'succeeded' WHERE id = %s::uuid", (first,))
    assert gate(db, tmp_path)["proceed"] is True


@pytest.mark.parametrize("cause", ["incident", "primary_failure_operation", "failback_operation"])
def test_scale_in_gate_refuses_during_a_primary_outage_even_with_a_candidate(db, sim, tmp_path, cause):
    provisioned(db, sim, tmp_path, "digitalocean", "warm")
    provisioned(db, sim, tmp_path, "linode")
    if cause == "incident":
        db.execute("SELECT * FROM ops.open_incident('primary.unhealthy:relief', 'critical', 'automation', 'down')")
    else:
        new_op(db, cause.replace("_operation", ""), "waiting")
    assert gate(db, tmp_path) == {"proceed": False, "noop_reason": "primary_outage"}
    db.execute("DELETE FROM ops.operations WHERE kind IN ('primary_failure', 'failback')")
    db.execute("SELECT ops.resolve_incident('primary.unhealthy:relief', 'ok')")
    assert gate(db, tmp_path)["proceed"] is True


# ------------------------------------------------------------------ failover_target
def test_failover_target_reserves_on_the_non_warm_provider_then_resumes_and_reuses(db, sim, tmp_path):
    op, gen = lease_of(db, "primary_failure")
    ok, out = make(FailoverTarget, tmp_path).run(op, gen)
    assert out["action"] == "reserved" and out["provider"] == "linode" and not out["fallback"]
    again = make(FailoverTarget, tmp_path).run(op, gen)[1]
    assert again["action"] == "resume" and again["slot_key"] == out["slot_key"]
    assert db.execute("SELECT count(*) FROM ops.capacity_slots WHERE role = 'elastic' AND state <> 'free'").fetchone()[0] == 1


def test_failover_target_never_matches_an_unbound_reserved_slot_it_waits_then_adopts_the_bound_one(db, sim, tmp_path):
    """Race: a scale_out reserved a slot but has not bound the instance yet. Reusing it would route to nothing (P0002)."""
    op, gen = lease_of(db, "scale_out")
    ok, r = make(ReserveSlot, tmp_path).run(op, gen, "linode", "elastic", True)
    key, sgen = r["slot_key"], r["slot_generation"]
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (op, gen))  # the in-flight op's lease lapsed/moved on; slot stays reserved
    op2, gen2 = lease_of(db, "primary_failure")
    out = make(FailoverTarget, tmp_path).run(op2, gen2, wait_seconds=0, poll_seconds=0)[1]
    assert out["action"] == "wait" and out["slot_key"] == key and out["held_by_operation"] == op  # not "reuse", no second reservation
    assert db.execute("SELECT count(*) FROM ops.capacity_slots WHERE role = 'elastic' AND state <> 'free'").fetchone()[0] == 1
    # the in-flight provision binds the instance: now there is something to adopt
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (op2, gen2))
    gen = db.execute("SELECT generation FROM ops.acquire_lease(%s::uuid)", (op,)).fetchone()[0]
    db.execute("SELECT * FROM ops.start_attempt(%s::uuid, %s, 1, 'linode', %s, %s)", (op, gen, key, sgen))
    res = sim.create("linode", key, op, 1, tags_for(key, op, 1), {})
    make(BindSlot, tmp_path).run(op, gen, key, sgen, res["resource_id"], res["ip"], 1, 1024)
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (op, gen))
    gen2 = db.execute("SELECT generation FROM ops.acquire_lease(%s::uuid)", (op2,)).fetchone()[0]
    reuse = make(FailoverTarget, tmp_path).run(op2, gen2, wait_seconds=0, poll_seconds=0)[1]
    assert reuse["action"] == "reuse" and reuse["slot_key"] == key and reuse["held_by_operation"] == op


def test_failover_target_falls_back_to_the_warm_provider_only_when_the_other_cannot_serve(db, tmp_path):
    cfg = json.dumps({"linode": {"unavailable": True}})
    faults = tmp_path / "faults.json"
    faults.write_text(cfg)
    op, gen = lease_of(db, "primary_failure")
    out = make(FailoverTarget, tmp_path, simulation_faults=str(faults)).run(op, gen)[1]
    assert out["action"] == "reserved" and out["provider"] == "digitalocean" and out["fallback"] is True
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (op, gen))
    db.execute("UPDATE ops.operations SET status = 'failed' WHERE id = %s::uuid", (op,))
    db.execute("UPDATE ops.capacity_slots SET state = 'free', bound_instance_id = NULL, lease_operation_id = NULL")
    faults.write_text(json.dumps({"linode": {"unavailable": True}, "digitalocean": {"unavailable": True}}))
    op2, gen2 = lease_of(db, "primary_failure")
    assert make(FailoverTarget, tmp_path, simulation_faults=str(faults)).run(op2, gen2)[1] == {"action": "none"}


def test_failover_target_respects_the_provider_cap(db, tmp_path):
    cfg = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "docs", "config", "policy-defaults.json")))
    cfg["capacity"]["max_cloud_vms_per_provider"] = {"digitalocean": 1, "linode": 0}
    db.execute("UPDATE ops.policy_versions SET configuration = %s::jsonb WHERE version = 1", (json.dumps(cfg),))
    provisioned_op, gen = lease_of(db, "scale_out")
    db.execute("SELECT * FROM ops.reserve_slot(%s::uuid, %s, 'digitalocean', 'warm', true)", (provisioned_op, gen))  # warm takes DO's only place
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (provisioned_op, gen))
    db.execute("UPDATE ops.operations SET status = 'failed' WHERE id = %s::uuid", (provisioned_op,))
    op, gen2 = lease_of(db, "primary_failure")
    assert make(FailoverTarget, tmp_path).run(op, gen2)[1]["action"] == "none"


# ------------------------------------------------------------------ routing, drain, delete
def test_apply_routing_writes_the_projection_and_the_sim_map_and_refuses_unready_servers(db, sim, tmp_path):
    key, _, _ = provisioned(db, sim, tmp_path, "digitalocean", "warm")
    elastic, _, _ = provisioned(db, sim, tmp_path, "linode", ready=False)
    op, gen = lease_of(db, "primary_failure")
    a = make(ApplyRouting, tmp_path)
    assert a.run(op, gen, "primary", "maint")[1]["simulated"] is True
    sgen = slot(db, key)[1]
    assert a.run(op, gen, key, "active", sgen)[1]["applied"]
    assert db.execute("SELECT routing_mode FROM ops.instances WHERE slot_key = %s", (key,)).fetchone()[0] == "active"
    assert sim.routing() == {"primary": "maint", key: "active"}
    assert a.run(op, gen, elastic, "active", slot(db, elastic)[1]) == (False, "invalid_transition")  # not ready: refused, sim map untouched
    assert sim.routing()[("primary")] == "maint" and elastic not in sim.routing()
    assert a.run(op, gen + 1, "primary", "normal") == (False, "lease_lost")
    assert a.run(op, gen, "primary", "bogus") == (False, "bad_argument")


def test_apply_routing_warm_resolves_the_bound_standby_or_reports_none(db, sim, tmp_path):
    op, gen = lease_of(db, "primary_failure")
    a = make(ApplyRouting, tmp_path)
    assert a.run(op, gen, "warm", "active")[1]["applied"] is False
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (op, gen))
    key, _, _ = provisioned(db, sim, tmp_path, "digitalocean", "warm")
    op, gen = lease_of(db, "primary_failure")
    out = a.run(op, gen, "warm", "active")[1]
    assert out["applied"] is True and out["target"] == key == "warm" and sim.routing() == {"warm": "active"}
    assert db.execute("SELECT routing_mode FROM ops.instances WHERE slot_key = 'warm'").fetchone()[0] == "active"


def test_apply_routing_refuses_the_real_adapter_without_haproxy_settings(db, tmp_path):
    op, gen = lease_of(db, "primary_failure")
    a = make(ApplyRouting, tmp_path, provider_adapter="real", real_adapter=REAL_CFG)
    assert a.run(op, gen, "primary", "maint") == (False, "provider_adapter_missing_or_unknown")  # AdapterConfigError: real_adapter.haproxy missing


def test_drain_instance_reports_drained_or_times_out_without_deleting(db, sim, tmp_path):
    _, rid, _ = provisioned(db, sim, tmp_path, "linode")
    op, gen = lease_of(db, "scale_in")
    assert make(DrainInstance, tmp_path).run(op, gen, "linode", rid, 5, 1)[1]["result"] == "drained"
    faults = tmp_path / "faults.json"
    faults.write_text(json.dumps({"linode": {"drain_busy": True}}))
    out = make(DrainInstance, tmp_path, simulation_faults=str(faults)).run(op, gen, "linode", rid, 2, 1)[1]
    assert out["result"] == "timeout" and out["last_state"] == "busy"
    assert sim.calls("delete") == []


def test_delete_slot_resource_proves_absence_and_refuses_unrecorded_or_extra_tagged_resources(db, sim, tmp_path):
    key, rid, _ = provisioned(db, sim, tmp_path, "linode")
    extra = sim.inject("linode", {"project": "cdr", "slot": key, "op": str(uuid.uuid4()), "attempt": "9"})
    op, gen = lease_of(db, "scale_in")
    out = make(DeleteSlotResource, tmp_path).run(op, gen, "linode", key, rid, 1)[1]
    assert out == {"result": "unknown", "reason": "unexpected_tagged_resource"} and sim.calls("delete") == []
    sim._txn(lambda s: s["resources"].pop(extra))
    out = make(DeleteSlotResource, tmp_path).run(op, gen, "linode", key, rid, 1)[1]
    assert out["result"] == "cleaned" and out["evidence"]["confirmed_absent"] is True and out["evidence"]["resource_ids"] == [rid]
    assert [c["resource_id"] for c in sim.calls("delete")] == [rid]
    assert make(DeleteSlotResource, tmp_path).run(op, gen + 1, "linode", key, rid, 1) == (False, "lease_lost")


def test_delete_slot_resource_never_deletes_when_the_provider_does_not_answer(db, sim, tmp_path):
    key, rid, _ = provisioned(db, sim, tmp_path, "linode")
    faults = tmp_path / "faults.json"
    faults.write_text(json.dumps({"linode": {"api_unknown": True}}))
    op, gen = lease_of(db, "scale_in")
    out = make(DeleteSlotResource, tmp_path, simulation_faults=str(faults)).run(op, gen, "linode", key, rid, 1)[1]
    assert out["result"] == "unknown" and [c for c in sim.calls("delete") if c["result"] == "ok"] == []


# ------------------------------------------------------------------ wait_lease, supersede
def test_wait_lease_acquires_waits_behind_a_live_holder_and_stops_when_superseded(db, tmp_path):
    holder, hgen = lease_of(db, "scale_out")
    waiter = new_op(db, "scale_in")
    out = make(WaitLease, tmp_path).run(waiter, "relief", 2, 1)[1]
    assert out == {"acquired": False, "reason": "timeout"}
    db.execute("UPDATE ops.operations SET status = 'succeeded' WHERE id = %s::uuid", (waiter,))
    assert make(WaitLease, tmp_path).run(waiter, "relief", 5, 1)[1] == {"acquired": False, "reason": "superseded"}
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (holder, hgen))
    other = new_op(db, "scale_out")
    got = make(WaitLease, tmp_path).run(other, "relief", 5, 1)[1]
    assert got["acquired"] is True and got["generation"] == hgen + 1


def test_wait_lease_does_not_take_an_expired_lease_of_another_operation(db, tmp_path):
    holder, hgen = lease_of(db, "scale_out")
    db.execute("UPDATE ops.control_leases SET lease_expires_at = now() - interval '1 second'")
    waiter = new_op(db, "primary_failure")
    assert make(WaitLease, tmp_path).run(waiter, "relief", 2, 1)[1] == {"acquired": False, "reason": "timeout"}
    assert db.execute("SELECT holder_operation::text FROM ops.control_leases").fetchone()[0] == holder  # only reconcile may take it


def test_supersede_queued_action(db, tmp_path):
    queued, running, by = new_op(db, "scale_in", "queued"), new_op(db, "scale_in"), new_op(db, "primary_failure")
    assert make(SupersedeQueued, tmp_path).run(by, "scale_in")[1] == {"superseded": [queued]}
    assert db.execute("SELECT status FROM ops.operations WHERE id = %s::uuid", (running,)).fetchone()[0] == "running"


# ------------------------------------------------------------------ adopt_attempt
def attempt_slot(db, tmp_path, provider="linode"):
    op, gen = lease_of(db, "scale_out")
    r = make(ReserveSlot, tmp_path).run(op, gen, provider, "elastic", True)[1]
    db.execute("SELECT * FROM ops.start_attempt(%s::uuid, %s, 1, %s, %s, %s)", (op, gen, provider, r["slot_key"], r["slot_generation"]))
    return op, gen, r["slot_key"], r["slot_generation"]


def test_adopt_attempt_classifies_by_provider_tags_and_never_creates(db, sim, tmp_path):
    op, gen, key, sgen = attempt_slot(db, tmp_path)
    a = make(AdoptAttempt, tmp_path)
    assert a.run(op, "linode", key, 1)[1]["result"] == "absent"
    res = sim.create("linode", key, op, 1, tags_for(key, op, 1), {})
    out = a.run(op, "linode", key, 1)[1]
    assert out["result"] == "present" and out["resource_id"] == res["resource_id"] and out["slot_state"] == "reserved"
    sim.inject("linode", tags_for(key, op, 1))
    assert a.run(op, "linode", key, 1)[1] == {"result": "unknown", "reason": "several_resources"}
    assert len(sim.calls("create")) == 1  # only the test's own create; the action never created
    db.execute("UPDATE ops.operation_attempts SET status = 'failed' WHERE operation_id = %s::uuid", (op,))
    assert a.run(op, "linode", key, 1)[1]["result"] == "failed"
    db.execute("UPDATE ops.operation_attempts SET status = 'uncertain' WHERE operation_id = %s::uuid", (op,))
    assert a.run(op, "linode", key, 1)[1]["result"] == "unknown"


def test_adopt_attempt_does_not_treat_an_unanswered_listing_as_absent(db, tmp_path):
    op, gen, key, sgen = attempt_slot(db, tmp_path)
    faults = tmp_path / "faults.json"
    faults.write_text(json.dumps({"linode": {"api_unknown": True}}))
    assert make(AdoptAttempt, tmp_path, simulation_faults=str(faults)).run(op, "linode", key, 1)[1] == {"result": "unknown", "reason": "list_unknown"}


# ------------------------------------------------------------------ alert_intent
class Intent(AlertIntent):
    def __init__(self):
        self.sent = []

    def _api(self):
        outer = self

        class Api:
            def run(self, ref, params):
                outer.sent.append((ref, params))
                return "exec-" + str(len(outer.sent))

        return Api()


def intent(tmp_path):
    a = Intent()
    a.config = {"supabase_ops_url": login_url(), "provider_adapter": "simulation", "simulation_state_dir": str(tmp_path)}
    return a


def alert(event, fp="a1", status="firing", version="1"):
    return {"status": status, "fingerprint": fp, "labels": {"alertname": "X", "cdr_event": event, "policy_version": version}}


def test_alert_intent_opens_one_operation_per_fingerprint_and_dispatches_it_once(db, tmp_path):
    a = intent(tmp_path)
    ok, out = a.run({"alerts": [alert("app.cpu.low")]})
    assert ok and out["intents"][0]["kind"] == "scale_in" and out["intents"][0]["created"] is True
    op = out["intents"][0]["operation_id"]
    assert a.sent == [("cdr.scale_in", {"operation_id": op})]
    assert db.execute("SELECT kind, status, policy_version FROM ops.operations WHERE id = %s::uuid", (op,)).fetchone() == ("scale_in", "queued", 1)
    again = a.run({"alerts": [alert("app.cpu.low")]})[1]
    assert again["intents"][0] == {"event": "app.cpu.low", "kind": "scale_in", "operation_id": op, "created": False}
    assert len(a.sent) == 1
    assert db.execute("SELECT count(*) FROM ops.operation_events WHERE operation_id = %s::uuid AND event_type = 'app.cpu.low'", (op,)).fetchone()[0] == 1


def test_alert_intent_stale_metrics_block_cpu_intents_but_not_the_primary_failure(db, tmp_path):
    a = intent(tmp_path)
    out = a.run({"alerts": [alert("metrics.stale", "s1"), alert("app.cpu.high", "h1"), alert("app.cpu.low", "l1"), alert("app.primary.unhealthy", "p1")]})[1]
    assert out["stale"] is True
    by = {i["event"]: i for i in out["intents"]}
    assert by["app.cpu.high"] == {"event": "app.cpu.high", "skipped": "metrics_stale"} and by["app.cpu.low"]["skipped"] == "metrics_stale"
    assert by["app.primary.unhealthy"]["kind"] == "primary_failure"
    assert [s[0] for s in a.sent] == ["cdr.primary_failure"]
    out = a.run({"alerts": [alert("metrics.stale", "s1", "resolved")]})[1]
    assert out["stale"] is False
    assert a.run({"alerts": [alert("app.cpu.low", "l2")]})[1]["intents"][0]["kind"] == "scale_in"


def test_alert_intent_haproxy_standby_alert_dispatches_standby_repair_by_label_or_alertname(db, tmp_path):
    a = intent(tmp_path)
    out = a.run({"alerts": [alert("standby.unhealthy", "w1")]})[1]["intents"][0]
    assert out["kind"] == "standby_repair" and out["created"] is True
    assert a.sent == [("cdr.standby_repair", {"operation_id": out["operation_id"]})]
    by_name = {"status": "firing", "fingerprint": "w2", "labels": {"alertname": "CdrStandbyUnhealthy", "policy_version": "1"}}
    assert a.run({"alerts": [by_name]})[1]["intents"][0]["kind"] == "standby_repair"


def test_alert_intent_primary_unhealthy_opens_the_incident_and_supersedes_a_queued_scale_in(db, tmp_path):
    a = intent(tmp_path)
    queued = a.run({"alerts": [alert("app.cpu.low", "l1")]})[1]["intents"][0]["operation_id"]
    out = a.run({"alerts": [alert("app.primary.unhealthy", "p1")]})[1]["intents"][0]
    assert out["superseded"] == [queued]
    assert db.execute("SELECT status, outcome ->> 'noop_reason' FROM ops.operations WHERE id = %s::uuid", (queued,)).fetchone() == ("succeeded", "superseded")
    assert db.execute("SELECT count(*) FROM ops.incidents WHERE fingerprint = 'primary.unhealthy:relief' AND state = 'open'").fetchone()[0] == 1


def test_alert_intent_cpu_high_activates_an_idle_ready_standby_first_then_scales_out(db, sim, tmp_path):
    a = intent(tmp_path)
    assert a.run({"alerts": [alert("app.cpu.high", "h1")]})[1]["intents"][0]["kind"] == "scale_out"  # no standby yet
    db.execute("UPDATE ops.operations SET status = 'failed'")
    key, _, _ = provisioned(db, sim, tmp_path, "digitalocean", "warm")
    assert a.run({"alerts": [alert("app.cpu.high", "h2")]})[1]["intents"][0]["kind"] == "activate_standby"
    db.execute("UPDATE ops.instances SET routing_mode = 'active' WHERE slot_key = %s", (key,))
    db.execute("UPDATE ops.operations SET status = 'failed'")
    assert a.run({"alerts": [alert("app.cpu.high", "h3")]})[1]["intents"][0]["kind"] == "scale_out"
    db.execute("UPDATE ops.operations SET status = 'failed'")
    db.execute("UPDATE ops.policy_versions SET configuration = jsonb_set(configuration, '{scale_out,activate_warm_standby_first}', 'false')")
    db.execute("UPDATE ops.instances SET routing_mode = 'backup' WHERE slot_key = %s", (key,))
    assert a.run({"alerts": [alert("app.cpu.high", "h4")]})[1]["intents"][0]["kind"] == "scale_out"


def test_alert_intent_a_failed_dispatch_leaves_the_operation_queued_and_is_reported(db, tmp_path):
    class Down(Intent):
        def _api(self):
            raise St2Unavailable("down")

    a = Down()
    a.config = {"supabase_ops_url": login_url()}
    out = a.run({"alerts": [alert("app.cpu.low")]})[1]["intents"][0]
    assert out["dispatch_error"] == "down" and db.execute("SELECT status FROM ops.operations WHERE id = %s::uuid", (out["operation_id"],)).fetchone()[0] == "queued"


def test_alert_intent_rejects_a_malformed_payload_and_ignores_unknown_alerts(db, tmp_path):
    a = intent(tmp_path)
    assert a.run({"alerts": "x"}) == (False, "bad_payload")
    ok, out = a.run({"alerts": [{"status": "firing", "fingerprint": "ab", "labels": {"alertname": "CdrBackupStale", "policy_version": "1"}}]})
    assert ok and out["intents"] == [] and out["recorded"] == 1
    assert a.run({"alerts": [{"status": "firing", "fingerprint": "ab", "labels": {"alertname": "CdrCpuHigh", "policy_version": "1"}}]})[1]["intents"][0]["kind"] == "scale_out"  # by alertname


# ------------------------------------------------------------------ control_state
class Store:
    def __init__(self):
        self.v = {}

    def get_value(self, k, local=True):
        return self.v.get(k)

    def set_value(self, k, v, local=True):
        self.v[k] = v

    def delete_value(self, k, local=True):
        self.v.pop(k, None)


def test_control_state_remembers_the_first_failure_and_settles_it_into_the_ops_records(db, tmp_path):
    a = make(ControlState, tmp_path)
    a.action_service = Store()
    assert a.run("down")[0] and a.run("down")[0]
    first = a.action_service.v["control_state_unavailable_since"]
    assert a.run("down") and a.action_service.v["control_state_unavailable_since"] == first
    op = new_op(db, "reconcile")
    ok, out = a.run("recover", op)
    assert ok and out["was_down"] is True and out["since"] == first
    assert a.action_service.v == {}
    events = [r[0] for r in db.execute("SELECT event_type FROM ops.operation_events WHERE operation_id = %s::uuid ORDER BY sequence", (op,))]
    assert events == ["control_state.unavailable", "control_state.recovered"]
    assert db.execute("SELECT state FROM ops.incidents WHERE fingerprint = 'control_state.unavailable:relief'").fetchone()[0] == "resolved"
    assert a.run("recover", op)[1] == {"was_down": False}
    assert a.run("recover") == (False, "bad_argument")


# ------------------------------------------------------------------ reconcile_pass
def rpass(db, tmp_path, api=None, faults=None, lease=True, previous_holder="", **cfg):
    op, gen = lease_of(db, "reconcile")
    a = make(ReconcilePass, tmp_path, **({"simulation_faults": str(faults)} if faults else {}), **cfg)
    a._api = (lambda: api) if api is not None else (lambda: (_ for _ in ()).throw(St2Unavailable("no api")))
    ok, out = a.run(op, gen if lease else 0, previous_holder, "relief")
    assert ok, out
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (op, gen))  # a tick ends with the lease released
    db.execute("UPDATE ops.operations SET status = 'succeeded' WHERE id = %s::uuid", (op,))
    return op, gen, out


def test_reconcile_adopts_a_live_execution_that_is_still_young_and_changes_nothing(db, sim, tmp_path):
    op = new_op(db, "scale_out")
    api = FakeApi()
    api.live[("cdr.scale_out", op)] = True
    _, _, out = rpass(db, tmp_path, api)
    assert out["adopted"] == [op] and not out["resumes"] and not out["interrupted"] and not api.cancelled
    assert db.execute("SELECT status FROM ops.operations WHERE id = %s::uuid", (op,)).fetchone()[0] == "running"


def test_reconcile_never_acts_on_an_operation_when_the_st2_api_cannot_answer(db, tmp_path):
    op = new_op(db, "scale_out", age_s=3600)
    _, _, out = rpass(db, tmp_path, None)
    assert out["st2_unknown"] == [op] and not out["interrupted"] and not out["resumes"]
    assert db.execute("SELECT status FROM ops.operations WHERE id = %s::uuid", (op,)).fetchone()[0] == "running"


def test_reconcile_resumes_a_dead_scale_out_that_holds_a_slot_under_the_same_operation_id(db, sim, tmp_path):
    op, gen, key, sgen = attempt_slot(db, tmp_path)
    db.execute("UPDATE ops.control_leases SET lease_expires_at = now() - interval '1 second'")  # the worker died holding it
    db.execute("UPDATE ops.operations SET stackstorm_execution_id = 'exec-old' WHERE id = %s::uuid", (op,))
    api = FakeApi()
    api.live[("cdr.scale_out", op)] = True  # the zombie execution is still "running" in st2
    rec, rgen = new_op(db, "reconcile"), None
    rgen = db.execute("SELECT generation FROM ops.acquire_lease(%s::uuid)", (rec,)).fetchone()
    assert rgen[0] == gen + 1  # takeover: a new fencing generation
    a = make(ReconcilePass, tmp_path)
    a._api = lambda: api
    ok, out = a.run(rec, rgen[0], op, "relief")
    assert ok and out["resumes"] == [{"ref": "cdr.scale_out", "parameters": {"operation_id": op, "resume": True, "first_attempt": 1, "attempts_done": {}}}]
    assert api.cancelled == [f"exec-{op[:8]}"]
    assert db.execute("SELECT status FROM ops.operations WHERE id = %s::uuid", (op,)).fetchone()[0] == "reconciling"
    assert slot(db, key)[0] == "reserved"  # untouched: the resumed run continues in it
    # the fenced worker's next lease-protected call fails
    assert db.execute("SELECT count(*) FROM ops.capacity_slots WHERE state <> 'free'").fetchone()[0] == 1
    with pytest.raises(psycopg.errors.Error) as err:
        db.execute("SELECT ops.assert_lease(%s::uuid, %s)", (op, gen))
    assert err.value.sqlstate == "CDR01"


def test_reconcile_resume_counts_finished_attempts_per_provider(db, sim, tmp_path):
    op, gen, key, sgen = attempt_slot(db, tmp_path)
    db.execute("UPDATE ops.operation_attempts SET status = 'cleaned' WHERE operation_id = %s::uuid", (op,))
    db.execute("SELECT * FROM ops.start_attempt(%s::uuid, %s, 2, 'linode', %s, %s)", (op, gen, key, sgen))
    db.execute("UPDATE ops.control_leases SET lease_expires_at = now() - interval '1 second'")
    _, _, out = rpass(db, tmp_path, FakeApi(), previous_holder=op)
    assert out["resumes"][0]["parameters"] == {"operation_id": op, "resume": True, "first_attempt": 2, "attempts_done": {"linode": 1}}


def test_reconcile_interrupts_a_dead_operation_it_cannot_resume_and_holds_its_reserved_slot_until_resolved(db, sim, tmp_path):
    op = new_op(db, "scale_in", age_s=1000)  # no execution, nothing to adopt
    _, _, out = rpass(db, tmp_path, FakeApi())
    assert out["interrupted"] == [op]
    assert db.execute("SELECT status, outcome ->> 'error' FROM ops.operations WHERE id = %s::uuid", (op,)).fetchone() == ("failed", "interrupted")


def test_reconcile_closes_a_stale_queued_intent_and_leaves_a_young_one(db, tmp_path):
    old, young = new_op(db, "scale_in", "queued", age_s=600), new_op(db, "scale_out", "queued", age_s=5)
    _, _, out = rpass(db, tmp_path, FakeApi())
    assert out["stale_queued"] == [old] and young in out["adopted"]
    assert db.execute("SELECT status, outcome ->> 'noop_reason' FROM ops.operations WHERE id = %s::uuid", (old,)).fetchone() == ("succeeded", "stale_intent")
    assert db.execute("SELECT status FROM ops.operations WHERE id = %s::uuid", (young,)).fetchone()[0] == "queued"


def held_slot(db, sim, tmp_path, provider="digitalocean", create=True, tag_op=None):
    """T09 shape: create returned ambiguous, the slot is held_uncertain, the attempt is uncertain."""
    op, gen, key, sgen = attempt_slot(db, tmp_path, provider)
    rid = sim.create(provider, key, tag_op or op, 1, tags_for(key, tag_op or op, 1), {})["resource_id"] if create else None
    db.execute("SELECT ops.mark_slot_uncertain(%s::uuid, %s, %s, %s, 'create_timeout_ambiguous')", (op, gen, key, sgen))
    db.execute("UPDATE ops.operation_attempts SET status = 'uncertain' WHERE operation_id = %s::uuid", (op,))
    db.execute("UPDATE ops.operations SET status = 'failed' WHERE id = %s::uuid", (op,))
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (op, gen))
    return op, key, rid


def test_reconcile_resolves_a_held_slot_to_bound_when_the_provider_lists_the_attempts_resource(db, sim, tmp_path):
    op, key, rid = held_slot(db, sim, tmp_path)
    _, _, out = rpass(db, tmp_path, FakeApi())
    assert out["resolved_slots"] == [{"slot": key, "outcome": "bound", "resource_id": rid}] and not out["orphans"]
    assert db.execute("SELECT s.state, i.provider_resource_id, i.lifecycle_state FROM ops.capacity_slots s JOIN ops.instances i ON i.id = s.bound_instance_id WHERE s.key = %s", (key,)).fetchone() == ("bound", rid, "failed")
    assert db.execute("SELECT status FROM ops.operation_attempts WHERE operation_id = %s::uuid", (op,)).fetchone()[0] == "failed"
    assert db.execute("SELECT count(*) FROM ops.incidents").fetchone()[0] == 0 and sim.calls("delete") == []


def test_reconcile_frees_a_held_slot_only_when_a_provider_listing_shows_it_absent(db, sim, tmp_path):
    op, key, _ = held_slot(db, sim, tmp_path, create=False)
    faults = tmp_path / "faults.json"
    faults.write_text(json.dumps({"digitalocean": {"api_unknown": True}}))
    _, _, out = rpass(db, tmp_path, FakeApi(), faults=faults)
    assert out["providers_skipped"] == ["digitalocean"] and slot(db, key)[0] == "held_uncertain"  # unknown is not absent
    _, _, out = rpass(db, tmp_path, FakeApi())
    assert out["resolved_slots"] == [{"slot": key, "outcome": "free"}] and slot(db, key)[0] == "free"
    assert db.execute("SELECT status FROM ops.operation_attempts WHERE operation_id = %s::uuid", (op,)).fetchone()[0] == "cleaned"


def test_reconcile_keeps_a_held_slot_whose_resource_tags_no_recorded_attempt_explains_and_raises_an_incident(db, sim, tmp_path):
    op, key, rid = held_slot(db, sim, tmp_path, tag_op=str(uuid.uuid4()))
    _, _, out = rpass(db, tmp_path, FakeApi())
    assert slot(db, key)[0] == "held_uncertain" and not out["resolved_slots"]
    assert db.execute("SELECT count(*) FROM ops.incidents WHERE fingerprint = %s AND state = 'open'", (f"orphan:digitalocean:{rid}",)).fetchone()[0] == 1
    assert sim.calls("delete") == []


def test_reconcile_holds_a_free_slot_for_an_unmatched_tagged_resource_and_never_deletes_it(db, sim, tmp_path):
    rid = sim.inject("digitalocean", {"project": "cdr", "slot": "elastic-do-3", "op": str(uuid.uuid4()), "attempt": "1"})
    _, _, out = rpass(db, tmp_path, FakeApi())
    assert out["orphans"] == [{"provider": "digitalocean", "resource_id": rid, "slot": "elastic-do-3", "slot_held": True}]
    assert slot(db, "elastic-do-3")[0] == "held_uncertain"
    assert db.execute("SELECT i.provider_resource_id, i.simulated FROM ops.instances i WHERE slot_key = 'elastic-do-3'").fetchone() == (rid, True)
    assert db.execute("SELECT count(*) FROM ops.incidents WHERE fingerprint = %s AND state = 'open' AND severity = 'critical'", (f"orphan:digitalocean:{rid}",)).fetchone()[0] == 1
    assert sim.calls("delete") == [] and sim.get("digitalocean", rid)["state"] == "present"
    # the next tick keeps it without incident spam: the held slot now explains the resource, one incident and one event, nothing deleted
    _, _, again = rpass(db, tmp_path, FakeApi())
    assert again["orphans"] == [] and not again["resolved_slots"]
    assert db.execute("SELECT count(*) FROM ops.incidents WHERE fingerprint = %s", (f"orphan:digitalocean:{rid}",)).fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM ops.incident_events e JOIN ops.incidents i ON i.id = e.incident_id WHERE i.fingerprint = %s", (f"orphan:digitalocean:{rid}",)).fetchone()[0] == 1
    assert slot(db, "elastic-do-3")[0] == "held_uncertain" and sim.calls("delete") == [] and sim.get("digitalocean", rid)["state"] == "present"


def test_reconcile_reports_an_untagged_slot_orphan_as_an_incident_without_inventing_a_slot(db, sim, tmp_path):
    rid = sim.inject("linode", {"project": "cdr", "op": str(uuid.uuid4())})
    _, _, out = rpass(db, tmp_path, FakeApi())
    assert out["orphans"][0]["slot_held"] is False and out["orphans"][0]["slot"] is None
    assert db.execute("SELECT count(*) FROM ops.capacity_slots WHERE state <> 'free'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM ops.incidents WHERE fingerprint = %s", (f"orphan:linode:{rid}",)).fetchone()[0] == 1
    rpass(db, tmp_path, FakeApi())  # the next tick: still reported in the summary, but no incident spam
    assert db.execute("SELECT count(*) FROM ops.incident_events e JOIN ops.incidents i ON i.id = e.incident_id WHERE i.fingerprint = %s", (f"orphan:linode:{rid}",)).fetchone()[0] == 1


def test_reconcile_does_not_flag_resources_that_a_slot_or_a_live_operation_explains(db, sim, tmp_path):
    key, rid, _ = provisioned(db, sim, tmp_path, "linode")
    live, gen, lkey, _ = attempt_slot(db, tmp_path, "digitalocean")
    inflight = sim.create("digitalocean", lkey, live, 1, tags_for(lkey, live, 1), {})
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (live, gen))
    api = FakeApi()
    api.live[("cdr.scale_out", live)] = True
    _, _, out = rpass(db, tmp_path, api)
    assert out["orphans"] == [] and out["adopted"] == [live] and inflight["resource_id"]


def test_reconcile_finishes_a_dead_scale_ins_release_only_on_confirmed_absence(db, sim, tmp_path):
    key, rid, creator = provisioned(db, sim, tmp_path, "linode")
    op, gen = lease_of(db, "scale_in")
    db.execute("SELECT ops.begin_slot_release(%s::uuid, %s, %s, %s)", (op, gen, key, slot(db, key)[1]))  # releasing, owned by the dead scale_in
    db.execute("UPDATE ops.control_leases SET lease_expires_at = now() - interval '1 second'")
    db.execute("UPDATE ops.operations SET updated_at = now() - interval '1000 seconds' WHERE id = %s::uuid", (op,))
    _, _, out = rpass(db, tmp_path, FakeApi(), previous_holder=op)
    assert out["interrupted"] == [op] and slot(db, key)[0] == "releasing" and out["released_slots"] == []
    assert db.execute("SELECT count(*) FROM ops.incidents WHERE fingerprint = %s AND state = 'open'", (f"release.pending:{key}",)).fetchone()[0] == 1
    assert sim.calls("delete") == []  # reconcile does not delete; the VM is still there
    sim.delete("linode", rid)  # an operator removed it
    _, _, out = rpass(db, tmp_path, FakeApi())
    assert out["released_slots"] == [key] and slot(db, key)[0] == "free"


def test_reconcile_observe_pass_reads_and_reports_but_writes_nothing(db, sim, tmp_path):
    stale = new_op(db, "scale_in", "queued", age_s=600)
    rid = sim.inject("digitalocean", {"project": "cdr", "slot": "elastic-do-3", "op": str(uuid.uuid4()), "attempt": "1"})
    holder, hgen = lease_of(db, "scale_out")  # a live lease is held elsewhere
    rec = new_op(db, "reconcile")
    a = make(ReconcilePass, tmp_path)
    a._api = lambda: FakeApi()
    ok, out = a.run(rec, 0, "", "relief")
    assert ok and out["observe_only"] is True and out["stale_queued"] == [stale] and out["orphans"][0]["resource_id"] == rid
    assert db.execute("SELECT status FROM ops.operations WHERE id = %s::uuid", (stale,)).fetchone()[0] == "queued"
    assert slot(db, "elastic-do-3")[0] == "free" and db.execute("SELECT count(*) FROM ops.incidents").fetchone()[0] == 0


def test_reconcile_pass_stops_when_its_own_lease_was_lost(db, sim, tmp_path):
    sim.inject("digitalocean", {"project": "cdr", "slot": "elastic-do-3", "op": str(uuid.uuid4()), "attempt": "1"})
    rec, gen = lease_of(db, "reconcile")
    a = make(ReconcilePass, tmp_path)
    a._api = lambda: FakeApi()
    assert a.run(rec, gen + 5, "", "relief") == (False, "lease_lost")
    assert slot(db, "elastic-do-3")[0] == "free"


# ------------------------------------------------------------------ reconcile with unconfigured providers (real adapter, no token)
def _real_reconcile(env, monkeypatch, db, tmp_path):
    import reconcile_pass
    from providers.real import RealAdapter

    monkeypatch.setattr(reconcile_pass.providers, "load", lambda _cfg: RealAdapter(env["cfg"]))
    return lambda: rpass(db, tmp_path, FakeApi())


def _unconfigured_incidents(db):
    return db.execute("SELECT fingerprint FROM ops.incidents WHERE fingerprint LIKE 'provider.unconfigured:%' AND state = 'open' ORDER BY fingerprint").fetchall()


def test_reconcile_with_both_providers_unconfigured_is_a_noop_with_one_incident_each(db, tmp_path, env, monkeypatch):
    monkeypatch.delenv("T_DO_TOKEN")
    monkeypatch.delenv("T_LI_TOKEN")
    run = _real_reconcile(env, monkeypatch, db, tmp_path)
    _, _, out = run()
    assert out["noop_reason"] == "providers_unconfigured" and sorted(out["providers_unconfigured"]) == ["digitalocean", "linode"]
    assert not out["orphans"] and not out["resolved_slots"] and not out["released_slots"]
    assert _unconfigured_incidents(db) == [("provider.unconfigured:digitalocean",), ("provider.unconfigured:linode",)]
    run()  # a recurring condition opens no second incident
    assert _unconfigured_incidents(db) == [("provider.unconfigured:digitalocean",), ("provider.unconfigured:linode",)]


@responses.activate
def test_reconcile_with_one_provider_unconfigured_scans_the_other_and_is_not_a_noop(db, tmp_path, env, monkeypatch):
    monkeypatch.delenv("T_DO_TOKEN")
    responses.add(responses.GET, re.compile(r"https://api\.linode\.com/v4/linode/instances.*"), json={"data": [], "pages": 1}, status=200)
    run = _real_reconcile(env, monkeypatch, db, tmp_path)
    _, _, out = run()
    assert "noop_reason" not in out and out["providers_unconfigured"] == ["digitalocean"] and out["providers_skipped"] == ["digitalocean"]
    assert _unconfigured_incidents(db) == [("provider.unconfigured:digitalocean",)]
    assert len(responses.calls) == 1 and "linode" in responses.calls[0].request.url


# ------------------------------------------------------------------ run_heavy restore_probe (T18 heavy half; needs the real pg_dump/pg_restore, same major as the server)
def _probe(db, tmp_path, gen_delta=0, lease=None, **cfg):
    from run_heavy import RunHeavy

    op, gen = lease or lease_of(db, "reconcile")
    a = make(RunHeavy, tmp_path, backup_db_url=ADMIN, restore_test_url=ADMIN, **cfg)
    return a.run("restore_probe", "heavy", op, gen + gen_delta)


def _scratch_dbs(db):
    return db.execute("SELECT datname FROM pg_database WHERE datname LIKE 'probe\\_%'").fetchall()


def test_restore_probe_restores_a_fresh_app_dump_into_a_scratch_database_and_cleans_up(db, tmp_path):
    import shutil

    assert shutil.which("pg_dump") and shutil.which("pg_restore"), "pg_dump/pg_restore (same major as the server) must be on PATH; this test never skips"
    ok, res = _probe(db, tmp_path)
    assert ok and res["result"] == "ok" and res["task"] == "restore_probe" and res["simulated"] is False, res
    assert res["tables_restored"] == res["tables_source"] > 0 and res["dump_bytes"] > 0
    assert _scratch_dbs(db) == []
    assert not any(p.startswith("cdr-probe-") for p in os.listdir(tempfile.gettempdir()))


def test_restore_probe_failures_are_short_codes_and_leave_nothing_behind(db, tmp_path):
    lease = lease_of(db, "reconcile")
    ok, res = _probe(db, tmp_path, lease=lease, pg_dump_bin="/nonexistent/pg_dump")
    assert ok and res == {"result": "failed", "code": "dump_failed", "task": "restore_probe", "resource_class": "heavy", "simulated": False}
    bad = tmp_path / "pg_restore"
    bad.write_text("#!/bin/sh\necho 'password=hunter2' >&2\nexit 1\n")
    bad.chmod(0o755)
    ok, res = _probe(db, tmp_path, lease=lease, pg_restore_bin=str(bad))
    assert ok and res["code"] == "restore_failed" and "hunter2" not in json.dumps(res)
    assert _scratch_dbs(db) == []


def test_restore_probe_by_a_fenced_worker_runs_no_tool(db, tmp_path):
    marker = tmp_path / "ran"
    tool = tmp_path / "pg_dump"
    tool.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    tool.chmod(0o755)
    assert _probe(db, tmp_path, gen_delta=1, pg_dump_bin=str(tool)) == (False, "lease_lost") and not marker.exists()


def test_restore_probe_without_its_settings_refuses(db, tmp_path):
    from run_heavy import RunHeavy

    op, gen = lease_of(db, "reconcile")
    assert make(RunHeavy, tmp_path).run("restore_probe", "heavy", op, gen) == (False, "restore_probe_config_missing")
