"""Capacity actions against a REAL migrated Postgres (TEST_DATABASE_URL, needs ops/0009), as a login role that
is a member of dr_ops AND dr_automation, which is how the StackStorm login is meant to be granted.
Run: pytest tests/ -q   (from the pack directory; psycopg and, for real runs, st2common installed)
"""
import json
import os
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from assert_lease import AssertLease
from acquire_lease import AcquireLease
from begin_slot_release import BeginSlotRelease
from cdr_db import SQLSTATE_CODES, classify
from release_lease import ReleaseLease
from release_slot import ReleaseSlot
from renew_lease import RenewLease
from reserve_slot import ReserveSlot
from update_ops_state import UpdateOpsState

ADMIN = os.environ.get("TEST_DATABASE_URL")
ROLE, PASSWORD = "cdr_pytest_st2", "pytest-only-not-a-secret"
EVIDENCE = {"confirmed_absent": True, "source": "provider_api", "observed_at": "2026-01-01T00:00:00Z"}

pytestmark = pytest.mark.skipif(not ADMIN, reason="needs TEST_DATABASE_URL (a database migrated through ops/0009)")


def login_url():
    u = urlsplit(ADMIN)
    return urlunsplit((u.scheme, f"{ROLE}:{PASSWORD}@{u.hostname}:{u.port}", u.path, u.query, u.fragment))


@pytest.fixture()
def db():
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN CREATE ROLE {ROLE} LOGIN; END IF; END $$")
        c.execute(f"ALTER ROLE {ROLE} PASSWORD '{PASSWORD}'")
        c.execute(f"GRANT dr_ops, dr_automation TO {ROLE}")
        c.execute("UPDATE ops.capacity_slots SET state = 'free', bound_instance_id = NULL, lease_operation_id = NULL")
        c.execute("DELETE FROM ops.operation_attempts")
        c.execute("DELETE FROM ops.instances")
        c.execute("UPDATE ops.control_leases SET holder_operation = NULL, lease_expires_at = NULL")
        c.execute("DELETE FROM ops.operation_events")
        c.execute("DELETE FROM ops.incident_events")
        c.execute("DELETE FROM ops.incidents")
        c.execute("DELETE FROM ops.processed_events")
        c.execute("DELETE FROM ops.operations")
        c.execute("DELETE FROM ops.policy_versions")
        c.execute("UPDATE ops.runtime_flags SET maintenance = false, metrics_stale_since = NULL")
        cfg = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "docs", "config", "policy-defaults.json")))
        c.execute("INSERT INTO ops.policy_versions (version, configuration, status, activation_phase) VALUES (1, %s, 'active', 'completed')", (json.dumps(cfg),))
        yield c


def act(cls):
    a = cls.__new__(cls)
    a.config = {"supabase_ops_url": login_url()}
    return a


def op_with_lease(db, kind="scale_out"):
    op = db.execute("INSERT INTO ops.operations (kind, policy_version, status) VALUES (%s, 1, 'running') RETURNING id::text", (kind,)).fetchone()[0]
    gen = db.execute("SELECT generation FROM ops.acquire_lease(%s::uuid)", (op,)).fetchone()[0]
    return op, gen


def test_classify_maps_only_the_project_sqlstates_and_never_leaks_driver_text():
    class E(Exception):
        sqlstate = "CDR01"

    assert classify(E()) == SQLSTATE_CODES["CDR01"] == "lease_lost"
    assert classify(RuntimeError("postgres://user:secret@host/db failed")) == "RuntimeError"


def test_config_missing_is_reported_without_connecting():
    a = AssertLease.__new__(AssertLease)
    a.config = {}
    assert a.run("00000000-0000-4000-8000-000000000000", 1) == (False, "config_missing")


def test_assert_lease_holder_passes_and_fenced_worker_gets_lease_lost(db):
    op, gen = op_with_lease(db)
    assert act(AssertLease).run(op, gen) == (True, {"asserted": True})
    assert act(AssertLease).run(op, gen + 1) == (False, "lease_lost")


def new_op(db, kind):
    return db.execute("INSERT INTO ops.operations (kind, policy_version, status) VALUES (%s, 1, 'running') RETURNING id::text", (kind,)).fetchone()[0]


def test_lease_actions_acquire_renew_release_and_report_a_held_lease_as_a_result(db):
    a, b = new_op(db, "scale_out"), new_op(db, "scale_out")
    ok, got = act(AcquireLease).run(a)
    assert ok and got["acquired"] is True and got["previous_holder"] is None
    gen = got["generation"]
    assert act(AcquireLease).run(b) == (True, {"acquired": False})  # live lease held by a
    assert act(AcquireLease).run(a)[1]["generation"] == gen  # re-entrant
    ok, renewed = act(RenewLease).run(a, gen)
    assert ok and renewed["renewed"] is True
    assert act(RenewLease).run(b, gen) == (False, "lease_lost")
    assert act(ReleaseLease).run(b, gen) == (True, {"released": False})
    assert act(ReleaseLease).run(a, gen) == (True, {"released": True})
    assert act(RenewLease).run(a, gen) == (False, "lease_lost")
    assert act(AcquireLease).run(b)[1]["generation"] == gen + 1


def test_expired_lease_is_taken_over_only_by_reconcile_and_the_old_worker_is_fenced(db):
    holder, scale, rec = new_op(db, "scale_out"), new_op(db, "scale_out"), new_op(db, "reconcile")
    gen = act(AcquireLease).run(holder)[1]["generation"]
    db.execute("UPDATE ops.control_leases SET lease_expires_at = now() - interval '1 second'")
    assert act(AcquireLease).run(scale) == (False, "slot_conflict")
    assert act(AssertLease).run(holder, gen) == (False, "lease_lost")  # expired, though nobody took it over
    ok, got = act(AcquireLease).run(rec)
    assert ok and got == {"acquired": True, "generation": gen + 1, "previous_holder": holder, "previous_expired": True}
    assert act(AssertLease).run(holder, gen) == (False, "lease_lost")
    assert act(AssertLease).run(rec, gen + 1) == (True, {"asserted": True})


def test_reserve_then_release_requires_evidence_and_the_slot_stays_counted_until_then(db):
    op, gen = op_with_lease(db)
    ok, r = act(ReserveSlot).run(op, gen, "linode", "elastic", True)
    assert ok and r["reserved"] and r["slot_key"] == "elastic-linode-1" and r["replayed"] is False
    again = act(ReserveSlot).run(op, gen, "linode", "elastic", True)
    assert again[1]["replayed"] is True and again[1]["slot_key"] == r["slot_key"]
    assert db.execute("SELECT simulated FROM ops.instances WHERE id = %s::uuid", (r["instance_id"],)).fetchone() == (True,)

    args = (op, gen, r["slot_key"], r["slot_generation"])
    assert act(BeginSlotRelease).run(*args) == (True, {"releasing": True})
    assert act(ReleaseSlot).run(*args, {"confirmed_absent": True, "source": "timeout", "observed_at": "2026-01-01T00:00:00Z"}) == (False, "bad_argument")
    assert db.execute("SELECT state FROM ops.capacity_slots WHERE key = %s", (r["slot_key"],)).fetchone() == ("releasing",)
    assert act(ReleaseSlot).run(*args, EVIDENCE) == (True, {"released": True})
    assert db.execute("SELECT state FROM ops.capacity_slots WHERE key = %s", (r["slot_key"],)).fetchone() == ("free",)


def test_reserve_reports_no_capacity_as_a_result_not_an_error(db):
    db.execute("UPDATE ops.policy_versions SET configuration = jsonb_set(configuration, '{capacity,max_cloud_vms_per_provider,linode}', '0')")
    op, gen = op_with_lease(db)
    assert act(ReserveSlot).run(op, gen, "linode", "elastic", True) == (True, {"reserved": False})


def test_update_ops_state_projects_status_and_event_and_rejects_bad_transitions(db):
    op = db.execute("INSERT INTO ops.operations (kind, policy_version, status) VALUES ('reconcile', 1, 'queued') RETURNING id::text").fetchone()[0]
    a = act(UpdateOpsState)
    ok, out = a.run(op, "operation.started", "begin", status="running", adapter_mode="simulation", source="reconciliation")
    assert ok and isinstance(out["sequence"], int)
    assert a.run(op, "operation.succeeded", "skip", status="succeeded") == (False, "invalid_transition")
    row = db.execute("SELECT status, current_step, adapter_mode FROM ops.operations WHERE id = %s::uuid", (op,)).fetchone()
    assert row == ("running", "begin", "simulation")
    assert db.execute("SELECT event_type, source FROM ops.operation_events WHERE operation_id = %s::uuid", (op,)).fetchall() == [("operation.started", "reconciliation")]
