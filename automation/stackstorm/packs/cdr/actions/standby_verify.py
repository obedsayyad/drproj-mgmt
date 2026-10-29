"""docs/04 repair step 6: `/health/ready` passes (readiness check), the deployed digest matches, and HAProxy shows the server UP as `backup`.
Simulation adapter: the HAProxy view is the simulation's routing map. Real adapter: HAProxy `show servers state` must show the slot's
backup line (warm_bk / iwarm_bk) UP and ready, the relief /health/ready answers ready from the instance's identity, and the instance's
recorded image_digest equals the primary's. verified=false lists which check failed."""
import time

import providers
from cdr_common import guarded
from cdr_db import call
from haproxy_routing import lines_for_slot
from st2common.runners.base_action import Action


UP_WAIT_SECONDS = 45
UP_POLL_SECONDS = 3


class StandbyVerify(Action):
    @guarded
    def run(self, operation_id, lease_generation, slot_key, provider, resource_id):
        adapter = providers.load(self.config)
        ok, rows = call(self.config, "SELECT i.lifecycle_state, i.image_digest, i.routing_mode, i.public_ip FROM ops.capacity_slots s JOIN ops.instances i ON i.id = s.bound_instance_id WHERE s.key = %s", (slot_key,))
        if not ok:
            return False, rows
        lifecycle, digest, routing_db, ip = rows[0] if rows else (None, None, None, None)
        if not adapter.simulated:
            return self._real(adapter, provider, resource_id, slot_key, lifecycle, digest, routing_db, ip)
        check = adapter.check_ready(provider, resource_id)
        pinned = adapter.pinned_digest()
        routing = adapter.routing().get(slot_key)
        checks = {
            "ready": check["state"] == "ready",
            "digest_matches": check.get("image_digest") == pinned and digest == pinned,
            "instance_ready": lifecycle == "ready",
            "routing_backup": routing_db == "backup" and routing == "backup",
        }
        return True, {"verified": all(checks.values()), "checks": checks, "simulated": adapter.simulated}

    def _real(self, adapter, provider, resource_id, slot_key, lifecycle, digest, routing_db, ip):
        ok, rows = call(self.config, "SELECT image_digest FROM ops.instances WHERE role = 'primary' AND image_digest IS NOT NULL ORDER BY last_seen_at DESC NULLS LAST LIMIT 1", ())
        if not ok:
            return False, rows
        pinned = rows[0][0] if rows else None
        check = adapter.check_ready(provider, resource_id, ip=ip)
        bk, _ = lines_for_slot(slot_key)
        # A backup server that was just pointed at the instance is DOWN until HAProxy's own health checks pass (rise x inter): give it a bounded
        # time instead of failing the repair on the first look.
        waited = 0
        while True:
            server = adapter.membership().servers.get(bk) if bk else None
            if (server is not None and server.is_up) or waited >= UP_WAIT_SECONDS:
                break
            time.sleep(UP_POLL_SECONDS)
            waited += UP_POLL_SECONDS
        checks = {
            "ready": check["state"] == "ready",
            "digest_matches": pinned is not None and digest == pinned,
            "instance_ready": lifecycle == "ready",
            "routing_backup": routing_db == "backup" and server is not None and server.is_up,
        }
        return True, {"verified": all(checks.values()), "checks": checks, "simulated": False, "adapter_mode": adapter.mode}
