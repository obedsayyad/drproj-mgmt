"""Atomically reserve one cloud slot (free -> reserved), counting toward the per-provider cap (docs/04, docs/06).

`reserved: false` means no capacity (cap reached, warm place held back, or no eligible slot); it is not an error.
`simulated` is required so a local adapter can never create an inventory row that looks real.
"""
from cdr_db import call
from st2common.runners.base_action import Action


class ReserveSlot(Action):
    def run(self, operation_id, lease_generation, provider, kind, simulated):
        ok, rows = call(
            self.config,
            "SELECT reserved_key, reserved_generation, reserved_instance_id::text, replayed"
            " FROM ops.reserve_slot(%s::uuid, %s, %s, %s, %s)",
            (operation_id, lease_generation, provider, kind, bool(simulated)),
        )
        if not ok:
            return False, rows
        if not rows:
            return True, {"reserved": False}
        key, generation, instance_id, replayed = rows[0]
        return True, {"reserved": True, "slot_key": key, "slot_generation": generation, "instance_id": instance_id, "replayed": replayed}
