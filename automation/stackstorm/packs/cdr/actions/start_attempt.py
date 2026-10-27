"""Record the provisioning attempt BEFORE the provider create call; created=false means a restart (reconcile by tags, do not create again)."""
from cdr_db import call
from st2common.runners.base_action import Action


class StartAttempt(Action):
    def run(self, operation_id, lease_generation, attempt, provider, slot_key, slot_generation):
        ok, rows = call(
            self.config,
            "SELECT attempt_id::text, created, deadline_at, checkpoints_at FROM ops.start_attempt(%s::uuid, %s, %s, %s, %s, %s)",
            (operation_id, lease_generation, attempt, provider, slot_key, slot_generation),
        )
        if not ok:
            return False, rows
        attempt_id, created, deadline, checkpoints = rows[0]
        return True, {"attempt_id": attempt_id, "created": created, "deadline_at": deadline.isoformat(), "checkpoints_at": [c.isoformat() for c in checkpoints]}
