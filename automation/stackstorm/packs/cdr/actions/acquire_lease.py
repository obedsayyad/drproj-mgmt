"""Acquire the capacity lease with a fencing generation (docs/07). Not acquiring is a result, not an error."""
from cdr_db import call
from st2common.runners.base_action import Action


class AcquireLease(Action):
    def run(self, operation_id, app_key="relief", ttl_seconds=120):
        ok, rows = call(
            self.config,
            "SELECT generation, previous_holder::text, previous_expired FROM ops.acquire_lease(%s::uuid, %s, %s)",
            (operation_id, app_key, ttl_seconds),
        )
        if not ok:
            return False, rows
        if not rows:
            return True, {"acquired": False}
        generation, previous_holder, previous_expired = rows[0]
        return True, {"acquired": True, "generation": generation, "previous_holder": previous_holder, "previous_expired": previous_expired}
