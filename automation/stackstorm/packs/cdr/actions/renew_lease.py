"""Heartbeat renewal of the capacity lease (docs/07: every 20 s)."""
from cdr_db import call
from st2common.runners.base_action import Action


class RenewLease(Action):
    def run(self, operation_id, generation, app_key="relief", ttl_seconds=120):
        ok, rows = call(self.config, "SELECT ops.renew_lease(%s::uuid, %s, %s, %s)", (operation_id, generation, app_key, ttl_seconds))
        return (True, {"renewed": True, "lease_expires_at": rows[0][0].isoformat()}) if ok else (False, rows)
