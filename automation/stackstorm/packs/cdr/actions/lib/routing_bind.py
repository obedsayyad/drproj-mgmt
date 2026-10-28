"""Binds the real adapter's HAProxy mutations to the operation: assert_lease before every command, ops audit record after it."""
import json

from cdr_db import call
from haproxy_admin import AdminError
from haproxy_routing import lines_for_slot
from lease_keeper import fenced


def bind(config, adapter, operation_id, generation):
    """Bind a real adapter; returns it. The simulation adapter has no bind and is returned untouched."""
    if getattr(adapter, "simulated", True) or not hasattr(adapter, "bind"):
        return adapter

    def fence():
        fenced(config, operation_id, generation, lambda: None)

    def audit(cmd, old, new, dump):
        ok, _ = call(config, "SELECT ops.record_haproxy_command(%s::uuid, %s, %s, %s::jsonb, %s::jsonb, %s)",
                     (operation_id, generation, cmd, json.dumps(old), json.dumps(new), dump))
        if not ok:
            raise AdminError("audit_failed")

    return adapter.bind(fence, audit)


def serving_lines(config):
    """Cloud HAProxy lines (warm_act, iwarm_act, elastic-*) of slots routed active or draining in ops: the lines that carry traffic."""
    ok, rows = call(config, "SELECT s.key FROM ops.capacity_slots s JOIN ops.instances i ON i.id = s.bound_instance_id "
                            "WHERE i.routing_mode IN ('active', 'draining')", ())
    if not ok:
        return False, rows
    return True, tuple(lines_for_slot(r[0])[1] for r in rows)


def slot_ip(config, slot_key):
    ok, rows = call(config, "SELECT i.public_ip FROM ops.capacity_slots s JOIN ops.instances i ON i.id = s.bound_instance_id WHERE s.key = %s", (slot_key,))
    if not ok:
        return False, rows
    return True, (rows[0][0] if rows else None)
