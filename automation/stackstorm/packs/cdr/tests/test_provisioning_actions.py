"""Provisioning actions against a REAL migrated Postgres (ops/0011) and the file-backed simulation adapter.
Fencing is the point: every side-effecting action asserts the lease immediately before the adapter call, so a fenced
worker leaves the call log with zero provider calls."""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from acquire_lease import AcquireLease
from adapter_info import AdapterInfo
from capacity_gate import CapacityGate
from fail_operation import FailOperation
from finish_attempt import FinishAttempt
from next_elastic_provider import NextElasticProvider
from open_incident import OpenIncident
from open_operation import OpenOperation
from plan_next_attempt import PlanNextAttempt
from provider_cleanup import ProviderCleanup
from provider_create import ProviderCreate
from release_slot import ReleaseSlot
from begin_slot_release import BeginSlotRelease
from bind_slot import BindSlot
from reserve_slot import ReserveSlot
from run_heavy import RunHeavy
from start_attempt import StartAttempt
from wait_ready import WaitReady
from providers.simulation import SimulationAdapter
from real_cfg import REAL_CFG
from test_capacity_actions import db, login_url  # noqa: F401  (fixture reuse)

OTHER_OP = "00000000-0000-4000-8000-000000000000"
@pytest.fixture()
def sim(tmp_path):
    return {"dir": str(tmp_path / "sim"), "faults": str(tmp_path / "faults.json")}


def make(cls, sim, **extra):
    a = cls.__new__(cls)
    a.config = {"supabase_ops_url": login_url(), "provider_adapter": "simulation", "simulation_state_dir": sim["dir"], "simulation_faults": sim["faults"], **extra}
    return a


def faults(sim, data):
    with open(sim["faults"], "w") as f:
        json.dump(data, f)


def calls(sim, method=None):
    return SimulationAdapter(sim["dir"], None).calls(method)


def holder(db, kind="scale_out"):
    op = db.execute("INSERT INTO ops.operations (kind, policy_version, status) VALUES (%s, 1, 'running') RETURNING id::text", (kind,)).fetchone()[0]
    return op, db.execute("SELECT generation FROM ops.acquire_lease(%s::uuid)", (op,)).fetchone()[0]


def reserved(db, sim, provider="digitalocean"):
    op, gen = holder(db)
    ok, r = make(ReserveSlot, sim).run(op, gen, provider, "elastic", True)
    assert ok and r["reserved"]
    return op, gen, r["slot_key"], r["slot_generation"]


def created(db, sim, provider="digitalocean"):
    op, gen, key, sgen = reserved(db, sim, provider)
    ok, res = make(ProviderCreate, sim).run(op, gen, provider, key, 1)
    assert ok and res["result"] == "ok"
    ok, _ = make(BindSlot, sim).run(op, gen, key, sgen, res["resource_id"], res["ip"], 1, 1024)
    assert ok
    return op, gen, key, sgen, res["resource_id"]


def test_create_asserts_the_lease_first_so_a_fenced_worker_creates_nothing(db, sim):
    op, gen, key, _ = reserved(db, sim)
    assert make(ProviderCreate, sim).run(op, gen + 1, "digitalocean", key, 1) == (False, "lease_lost")
    # an operation that does not exist has no captured policy (the keeper needs its renewal interval): refused before any provider call
    assert make(ProviderCreate, sim).run(OTHER_OP, gen, "digitalocean", key, 1) == (False, "policy_not_found")
    assert calls(sim, "create") == []
    ok, res = make(ProviderCreate, sim).run(op, gen, "digitalocean", key, 1)
    assert ok and res["result"] == "ok" and res["simulated"] is True
    assert [c["method"] for c in calls(sim)] == ["create"]
    tags = SimulationAdapter(sim["dir"], None).list_by_tag("digitalocean", {"project": "cdr", "slot": key, "op": op, "attempt": "1"})
    assert len(tags["resources"]) == 1


def test_create_timeout_is_a_result_not_an_error(db, sim):
    faults(sim, {"digitalocean": {"create_timeout": True}})
    op, gen, key, _ = reserved(db, sim)
    assert make(ProviderCreate, sim).run(op, gen, "digitalocean", key, 1) == (True, {"result": "timeout_ambiguous", "resource_id": None, "simulated": True})
    assert len(calls(sim, "create")) == 1


def test_provider_create_refuses_the_real_adapter_and_a_missing_adapter_is_a_config_error(db, sim):
    op, gen, key, _ = reserved(db, sim)
    # real without its real_adapter settings is a configuration error; with them, Terraform is reachable only through run_heavy
    assert make(ProviderCreate, sim, provider_adapter="real").run(op, gen, "digitalocean", key, 1) == (False, "provider_adapter_missing_or_unknown")
    assert make(ProviderCreate, sim, provider_adapter="real", real_adapter=REAL_CFG).run(op, gen, "digitalocean", key, 1) == (False, "terraform_via_run_heavy_only")
    missing = make(ProviderCreate, sim)
    del missing.config["provider_adapter"]
    assert missing.run(op, gen, "digitalocean", key, 1) == (False, "provider_adapter_missing_or_unknown")
    assert calls(sim) == []
    assert make(AdapterInfo, sim).run() == (True, {"adapter_mode": "simulation", "simulated": True})
    assert make(AdapterInfo, sim, provider_adapter="real", real_adapter=REAL_CFG).run() == (True, {"adapter_mode": "real", "simulated": False})


def test_instances_created_through_the_simulation_are_marked_simulated(db, sim):
    op, gen, key, *_ = reserved(db, sim)
    assert db.execute("SELECT simulated FROM ops.instances WHERE slot_key = %s", (key,)).fetchone()[0] is True
    ok, plan = make(PlanNextAttempt, sim).run(op, gen, "linode", {"linode": 1})  # holds a slot already: replay, still simulated
    assert db.execute("SELECT bool_and(simulated) FROM ops.instances").fetchone()[0] is True


def test_cleanup_deletes_only_this_attempts_tagged_resources_and_returns_release_evidence(db, sim):
    op, gen, key, sgen, rid = created(db, sim)
    bystander = SimulationAdapter(sim["dir"], None).create("digitalocean", "elastic-do-5", OTHER_OP, 1, {"project": "cdr", "slot": "elastic-do-5", "op": OTHER_OP, "attempt": "1"}, {})
    ok, out = make(ProviderCleanup, sim).run(op, gen, "digitalocean", key, 1, rid, 0)
    assert ok and out["result"] == "cleaned" and out["evidence"]["confirmed_absent"] is True and out["evidence"]["source"] == "provider_api"
    assert SimulationAdapter(sim["dir"], None).get("digitalocean", bystander["resource_id"])["state"] == "present"
    assert make(BeginSlotRelease, sim).run(op, gen, key, sgen) == (True, {"releasing": True})
    assert make(ReleaseSlot, sim).run(op, gen, key, sgen, out["evidence"]) == (True, {"released": True})
    assert db.execute("SELECT state FROM ops.capacity_slots WHERE key = %s", (key,)).fetchone()[0] == "free"


def test_cleanup_without_a_resource_id_finds_the_ambiguous_create_by_tags(db, sim):
    faults(sim, {"digitalocean": {"create_timeout": True}})
    op, gen, key, sgen = reserved(db, sim)
    make(ProviderCreate, sim).run(op, gen, "digitalocean", key, 1)
    faults(sim, {})
    ok, out = make(ProviderCleanup, sim).run(op, gen, "digitalocean", key, 1, None, 0)
    assert ok and out["result"] == "cleaned" and len(out["evidence"]["resource_ids"]) == 1


def test_cleanup_never_claims_absence_when_the_provider_does_not_answer(db, sim):
    op, gen, key, sgen, rid = created(db, sim)
    faults(sim, {"digitalocean": {"api_unknown": True}})
    ok, out = make(ProviderCleanup, sim).run(op, gen, "digitalocean", key, 1, rid, 0)
    assert ok and out["result"] == "unknown" and "evidence" not in out
    ok, out = make(ProviderCleanup, sim).run(op, gen, "digitalocean", key, 1, None, 0)
    assert ok and out["result"] == "unknown"
    faults(sim, {})
    assert SimulationAdapter(sim["dir"], None).get("digitalocean", rid)["state"] == "present"


def test_cleanup_by_a_fenced_worker_deletes_nothing(db, sim):
    op, gen, key, sgen, rid = created(db, sim)
    assert make(ProviderCleanup, sim).run(op, gen + 1, "digitalocean", key, 1, rid, 0) == (False, "lease_lost")
    assert calls(sim, "delete") == []


def deadlines(seconds, checkpoints):
    now = datetime.now(timezone.utc)
    return (now + timedelta(seconds=seconds)).isoformat(), [(now + timedelta(seconds=c)).isoformat() for c in checkpoints]


def waiting_events(db, op):
    return db.execute("SELECT count(*) FROM ops.operation_events WHERE operation_id = %s::uuid AND event_type = 'operation.waiting'", (op,)).fetchone()[0]


def test_wait_ready_admits_early_without_waiting_events(db, sim):
    faults(sim, {"digitalocean": {"ready_after_s": 0}})
    op, gen, key, sgen, rid = created(db, sim)
    assert make(RunHeavy, sim).run("deploy", "heavy", op, gen, "digitalocean", rid) [1]["result"] == "ok"
    deadline, cps = deadlines(30, [10, 20, 30])
    ok, out = make(WaitReady, sim).run(op, gen, "digitalocean", rid, deadline, cps, 0.05)
    assert ok and out["result"] == "ready" and out["image_digest"] and out["identity"] == rid
    assert waiting_events(db, op) == 0


def test_wait_ready_emits_one_waiting_event_per_checkpoint_then_reports_the_deadline(db, sim):
    faults(sim, {"digitalocean": {"never_ready": True}})
    op, gen, key, sgen, rid = created(db, sim)
    make(RunHeavy, sim).run("deploy", "heavy", op, gen, "digitalocean", rid)
    deadline, cps = deadlines(1.5, [0.5, 1.0, 1.5])
    ok, out = make(WaitReady, sim).run(op, gen, "digitalocean", rid, deadline, cps, 0.1)
    assert ok and out["result"] == "deadline" and out["checkpoints_emitted"] == 3
    assert waiting_events(db, op) == 3
    assert db.execute("SELECT status FROM ops.operations WHERE id = %s::uuid", (op,)).fetchone()[0] == "waiting"


def test_wait_ready_reports_unknown_when_the_provider_never_answers(db, sim):
    faults(sim, {"digitalocean": {"api_unknown": True}})
    op, gen, key, sgen, rid = created(db, sim)
    deadline, cps = deadlines(0.5, [0.5])
    assert make(WaitReady, sim).run(op, gen, "digitalocean", rid, deadline, cps, 0.1)[1]["result"] == "unknown"


class _RealLike:
    """Simulation answers behind the real adapter's contract: not simulated, readiness only with the address and without a digest."""

    simulated = False
    mode = "real"

    def __init__(self, inner):
        self._inner, self.ips = inner, []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def check_ready(self, provider, resource_id, ip=None):
        self.ips.append(ip)
        return {"state": "ready", "identity": resource_id} if ip else {"state": "unknown"}


def test_wait_ready_real_adapter_probes_the_instance_address_and_takes_the_recorded_digest(db, sim, monkeypatch):
    import wait_ready

    faults(sim, {"digitalocean": {"ready_after_s": 0}})
    op, gen, key, sgen, rid = created(db, sim)
    db.execute("UPDATE ops.instances SET public_ip = '203.0.113.9/32', image_digest = 'sha256:" + "b" * 64 + "' WHERE provider_resource_id = %s", (rid,))
    real = _RealLike(SimulationAdapter(sim["dir"], None))
    monkeypatch.setattr(wait_ready.providers, "load", lambda _cfg: real)
    deadline, cps = deadlines(30, [10, 20, 30])
    ok, out = make(WaitReady, sim).run(op, gen, "digitalocean", rid, deadline, cps, 0.05)
    assert ok and out["result"] == "ready" and out["image_digest"] == "sha256:" + "b" * 64 and out["identity"] == rid
    assert real.ips == ["203.0.113.9"]


def test_run_heavy_asserts_the_lease_before_deploying(db, sim):
    op, gen, key, sgen, rid = created(db, sim)
    assert make(RunHeavy, sim).run("deploy", "heavy", op, gen + 1, "digitalocean", rid) == (False, "lease_lost")
    assert calls(sim, "deploy") == []
    assert make(RunHeavy, sim).run("shell", "heavy", op, gen, "digitalocean", rid) == (False, "bad_argument")
    assert make(RunHeavy, sim).run("ansible_deploy", "heavy", op, gen + 1, "digitalocean", rid) == (False, "lease_lost")
    assert calls(sim, "deploy") == []
    assert make(RunHeavy, sim).run("terraform_apply", "heavy", op, gen, "digitalocean", slot_key=key) == (False, "bad_argument")  # exists is required
    assert make(RunHeavy, sim).run("terraform_apply", "heavy", op, gen + 1, "digitalocean", slot_key=key, exists=True) == (False, "lease_lost")
    assert len(calls(sim, "create")) == 1  # only the fixture's own create: the fenced and rejected calls created nothing


def test_run_heavy_terraform_apply_in_simulation_creates_through_the_adapter_and_delete_changes_nothing(db, sim):
    op, gen, key, sgen = reserved(db, sim)
    ok, res = make(RunHeavy, sim).run("terraform_apply", "heavy", op, gen, "digitalocean", slot_key=key, attempt=1, exists=True)
    assert ok and res["result"] == "ok" and res["resource_id"].startswith("sim-digitalocean-") and res["simulated"] is True and res["task"] == "terraform_apply"
    assert [c["method"] for c in calls(sim)] == ["create"]
    ok, res = make(RunHeavy, sim).run("terraform_apply", "heavy", op, gen, "digitalocean", slot_key=key, attempt=1, exists=False)
    assert ok and res["result"] == "ok" and res["note"] == "no_terraform_in_simulation"
    assert [c["method"] for c in calls(sim)] == ["create"]
    ok, out = make(RunHeavy, sim).run("terraform_output", "heavy", op, gen, "digitalocean", slot_key=key)
    assert ok and out["resource_id"] == "sim-digitalocean-1"


def test_plan_retries_same_provider_once_then_alternates_then_is_exhausted(db, sim):
    op, gen, key, sgen, rid = created(db, sim)
    cleanup = make(ProviderCleanup, sim).run(op, gen, "digitalocean", key, 1, rid, 0)[1]
    make(BeginSlotRelease, sim).run(op, gen, key, sgen)
    make(ReleaseSlot, sim).run(op, gen, key, sgen, cleanup["evidence"])
    ok, p = make(PlanNextAttempt, sim).run(op, gen, "digitalocean", {"digitalocean": 1})
    assert ok and p["plan"] == "retry_same" and p["provider"] == "digitalocean" and p["attempt"] == 2 and p["slot_key"].startswith("elastic-do-")
    db.execute("UPDATE ops.capacity_slots SET state = 'free', bound_instance_id = NULL, lease_operation_id = NULL WHERE key = %s", (p["slot_key"],))
    db.execute("DELETE FROM ops.instances")
    ok, p = make(PlanNextAttempt, sim).run(op, gen, "digitalocean", {"digitalocean": 2})
    assert ok and p["plan"] == "alternate" and p["provider"] == "linode" and p["attempt"] == 3
    db.execute("UPDATE ops.capacity_slots SET state = 'free', bound_instance_id = NULL, lease_operation_id = NULL")
    db.execute("DELETE FROM ops.instances")
    assert make(PlanNextAttempt, sim).run(op, gen, "linode", {"digitalocean": 2, "linode": 2})[1] == {"plan": "exhausted", "attempts": 4}


def test_plan_skips_an_unavailable_alternate_provider(db, sim):
    faults(sim, {"linode": {"unavailable": True}})
    op, gen = holder(db)
    assert make(PlanNextAttempt, sim).run(op, gen, "digitalocean", {"digitalocean": 2})[1]["plan"] == "exhausted"


def test_next_elastic_provider_skips_providers_the_adapter_cannot_see(db, sim):
    assert make(NextElasticProvider, sim).run()[1]["provider"] in ("digitalocean", "linode")
    faults(sim, {"digitalocean": {"unavailable": True}})
    ok, out = make(NextElasticProvider, sim).run()
    assert ok and out["provider"] == "linode" and out["skipped"] == ["digitalocean"] and out["simulated"] is True
    faults(sim, {"digitalocean": {"unavailable": True}, "linode": {"api_unknown": True}})
    assert make(NextElasticProvider, sim).run()[1]["provider"] is None


def test_capacity_gate_refuses_on_maintenance_cooldown_and_stale_metrics_and_otherwise_proceeds(db, sim):
    op, gen = holder(db)
    gate = lambda: make(CapacityGate, sim).run(op)[1]  # noqa: E731
    assert gate() == {"proceed": True, "metrics_source": "simulation"}
    SimulationAdapter(sim["dir"], None).set_metrics_fresh(False)
    assert gate() == {"proceed": False, "noop_reason": "metrics_stale", "metrics_source": "simulation"}
    SimulationAdapter(sim["dir"], None).set_metrics_fresh(True)
    prior = db.execute("INSERT INTO ops.operations (kind, policy_version, status, finished_at, outcome) VALUES ('scale_out', 1, 'succeeded', now(), '{}') RETURNING id").fetchone()[0]
    assert gate()["noop_reason"] == "cooldown"
    db.execute("UPDATE ops.operations SET outcome = '{\"noop_reason\": \"cooldown\"}' WHERE id = %s", (prior,))  # a no-op does not start a cooldown
    assert gate()["proceed"] is True
    db.execute("UPDATE ops.runtime_flags SET maintenance = true")
    try:
        assert gate() == {"proceed": False, "noop_reason": "maintenance"}
    finally:
        db.execute("UPDATE ops.runtime_flags SET maintenance = false")


def test_capacity_gate_in_real_mode_without_a_metrics_source_is_stale(db, sim):
    op, gen = holder(db)
    ok, out = make(CapacityGate, sim, provider_adapter="real", real_adapter=REAL_CFG).run(op)
    assert ok and out == {"proceed": False, "noop_reason": "metrics_stale", "metrics_source": "none"}


def test_open_operation_incident_and_attempt_actions_pass_through(db, sim):
    ok, a = make(OpenOperation, sim).run("scale_out", "alertmanager", "cpu.high:test", "AppCpuHigh")
    assert ok and a["created"] is True and a["policy_version"] == 1
    assert make(OpenOperation, sim).run("scale_out", "alertmanager", "cpu.high:test")[1]["operation_id"] == a["operation_id"]
    assert make(OpenIncident, sim).run("fp1", "critical", "automation", "first", ["linode"], a["operation_id"])[1]["created"] is True
    assert make(OpenIncident, sim).run("fp1", "critical", "automation", "again", ["linode"], a["operation_id"])[1]["created"] is False
    op, gen, key, sgen = reserved(db, sim)
    ok, s = make(StartAttempt, sim).run(op, gen, 1, "digitalocean", key, sgen)
    assert ok and s["created"] is True and len(s["checkpoints_at"]) == 3
    assert make(StartAttempt, sim).run(op, gen, 1, "digitalocean", key, sgen)[1]["created"] is False
    assert make(FinishAttempt, sim).run(op, gen, 1, "ready") == (True, {"status": "ready"})
    assert make(FinishAttempt, sim).run(op, gen, 1, "cleaned") == (False, "invalid_transition")


def test_fail_operation_keeps_a_fenced_worker_out_of_the_lease_and_releases_it_otherwise(db, sim):
    op, gen = holder(db)
    assert make(FailOperation, sim).run(op, gen, {"errors": [{"result": "lease_lost"}]}, "x") == (True, {"status": "reconciling"})
    assert db.execute("SELECT status FROM ops.operations WHERE id = %s::uuid", (op,)).fetchone()[0] == "reconciling"
    assert db.execute("SELECT holder_operation::text FROM ops.control_leases").fetchone()[0] == op  # not released by the fenced worker
    db.execute("UPDATE ops.control_leases SET holder_operation = NULL, lease_expires_at = NULL")
    op3, gen3 = holder(db)
    assert make(FailOperation, sim).run(op3, gen3, {"result": "boom"}, "x") == (True, {"status": "failed", "released": True})
    assert db.execute("SELECT status FROM ops.operations WHERE id = %s::uuid", (op3,)).fetchone()[0] == "failed"
    assert db.execute("SELECT holder_operation FROM ops.control_leases").fetchone()[0] is None


def test_fail_operation_still_releases_the_lease_when_the_operation_is_already_terminal(db, sim):
    """A failure after the operation was recorded succeeded (for example the release task) must not strand the lease."""
    op, gen = holder(db)
    db.execute("UPDATE ops.operations SET status = 'succeeded', finished_at = now(), outcome = '{\"noop_reason\": \"x\"}' WHERE id = %s::uuid", (op,))
    ok, out = make(FailOperation, sim).run(op, gen, "connect_or_network", "release")
    assert ok and out["released"] is True
    assert db.execute("SELECT holder_operation FROM ops.control_leases").fetchone()[0] is None
    assert db.execute("SELECT status FROM ops.operations WHERE id = %s::uuid", (op,)).fetchone()[0] == "succeeded"  # a terminal state is never rewritten


def test_fail_operation_reports_failure_when_the_lease_cannot_be_released_so_the_task_retries(db, sim):
    op, gen = holder(db)
    ok, out = make(FailOperation, sim).run(op, gen + 5, "boom", "x")  # wrong generation: release_lease says released=false
    assert ok and out["released"] is False  # fenced worker: nothing of ours to release, not an error
    a = make(FailOperation, sim)
    a.config = {**a.config, "supabase_ops_url": "postgres://nobody:x@127.0.0.1:1/none"}
    assert a.run(op, gen, "boom", "x") == (False, "connect_or_network")  # DB unreachable: failing the task lets the workflow retry it
