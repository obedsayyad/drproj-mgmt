"""Ensure exactly ONE elastic VM after a primary failure (docs/04). Order: the slot this operation already holds (resume) ->
any BOUND elastic slot with an instance (reuse: do not duplicate; an unbound reserved slot is never matched because
there is nothing to route to yet: wait for the in-flight provision, bounded, and adopt it once bound) -> reserve on the provider that is not the warm provider ->
the warm provider only if that one is at its cap or unavailable. reserve_slot enforces the cap atomically; no row means
no capacity. action: resume | reuse | reserved | wait | none."""
import time

import providers
from cdr_common import guarded, policy_section
from cdr_db import call
from st2common.runners.base_action import Action

OTHER = {"digitalocean": "linode", "linode": "digitalocean"}


class FailoverTarget(Action):
    @guarded
    def run(self, operation_id, lease_generation, wait_seconds=60, poll_seconds=5):
        adapter = providers.load(self.config)
        ok, rows = call(
            self.config,
            "SELECT key, generation, provider, state FROM ops.capacity_slots WHERE lease_operation_id = %s::uuid AND state IN ('reserved', 'bound') ORDER BY key LIMIT 1",
            (operation_id,),
        )
        if not ok:
            return False, rows
        if rows:
            key, gen, provider, state = rows[0]
            return True, {"action": "resume", "slot_key": key, "slot_generation": gen, "provider": provider, "slot_state": state}
        end = time.monotonic() + wait_seconds
        while True:
            ok, rows = call(
                self.config,
                "SELECT s.key, s.provider, s.state, s.lease_operation_id::text FROM ops.capacity_slots s"
                " JOIN ops.instances i ON i.id = s.bound_instance_id"
                " WHERE s.role = 'elastic' AND s.state = 'bound' ORDER BY s.key LIMIT 1",
                (),
            )
            if not ok:
                return False, rows
            if rows:
                key, provider, state, holder = rows[0]
                return True, {"action": "reuse", "slot_key": key, "provider": provider, "slot_state": state, "held_by_operation": holder}
            ok, rows = call(
                self.config,
                "SELECT key, lease_operation_id::text FROM ops.capacity_slots WHERE role = 'elastic' AND state IN ('reserved', 'bound') ORDER BY key LIMIT 1",
                (),
            )
            if not ok:
                return False, rows
            if not rows:
                break  # nothing in flight: reserve one below
            if time.monotonic() >= end:
                return True, {"action": "wait", "slot_key": rows[0][0], "held_by_operation": rows[0][1]}
            time.sleep(poll_seconds)
            # the wait must not let our own lease lapse
            ok, out = call(self.config, "SELECT ops.renew_lease(%s::uuid, %s, 'relief', 120)", (operation_id, lease_generation), attempts=1)
            if not ok and out == "lease_lost":
                return False, out
        ok, cap = policy_section(self.config, operation_id, "capacity")
        if not ok:
            return False, cap
        warm = cap["warm_provider"]
        for candidate in (OTHER[warm], warm):
            if adapter.observe(candidate) != "available":
                continue
            ok, rows = call(
                self.config,
                "SELECT reserved_key, reserved_generation FROM ops.reserve_slot(%s::uuid, %s, %s, 'elastic', %s)",
                (operation_id, lease_generation, candidate, adapter.simulated),
            )
            if not ok:
                return False, rows
            if rows:
                return True, {"action": "reserved", "provider": candidate, "slot_key": rows[0][0], "slot_generation": rows[0][1], "fallback": candidate == warm}
        return True, {"action": "none"}
