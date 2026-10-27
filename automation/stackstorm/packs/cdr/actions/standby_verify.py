"""docs/04 repair step 6: `/health/ready` passes (readiness check), the deployed digest matches, and HAProxy shows the server UP as `backup`.
In P4 the HAProxy view is the simulation's routing map (the real adapter reports not_implemented: P6). verified=false lists which check failed."""
import providers
from cdr_common import guarded
from cdr_db import call
from st2common.runners.base_action import Action


class StandbyVerify(Action):
    @guarded
    def run(self, operation_id, lease_generation, slot_key, provider, resource_id):
        adapter = providers.load(self.config)
        check = adapter.check_ready(provider, resource_id)
        ok, rows = call(self.config, "SELECT i.lifecycle_state, i.image_digest, i.routing_mode FROM ops.capacity_slots s JOIN ops.instances i ON i.id = s.bound_instance_id WHERE s.key = %s", (slot_key,))
        if not ok:
            return False, rows
        lifecycle, digest, routing_db = rows[0] if rows else (None, None, None)
        pinned = adapter.pinned_digest()
        if not adapter.simulated:
            raise NotImplementedError("not_implemented")
        routing = adapter.routing().get(slot_key)
        checks = {
            "ready": check["state"] == "ready",
            "digest_matches": check.get("image_digest") == pinned and digest == pinned,
            "instance_ready": lifecycle == "ready",
            "routing_backup": routing_db == "backup" and routing == "backup",
        }
        return True, {"verified": all(checks.values()), "checks": checks, "simulated": adapter.simulated}
