"""Find a bound, ready elastic VM that no routing was ever applied to (routing_mode none): scale_out joins it to HAProxy before growing the
fleet further. Covers a crash between 'ready' and the routing step, and VMs created before scale_out routed what it provisioned.
Returns found=false, or found=true with the slot (key, generation), provider and provider resource id."""
from cdr_common import guarded
from cdr_db import call
from st2common.runners.base_action import Action

SQL = (
    "SELECT c.key, c.generation, c.provider, i.provider_resource_id FROM ops.capacity_slots c JOIN ops.instances i ON i.id = c.bound_instance_id"
    " WHERE c.role = 'elastic' AND c.state = 'bound' AND i.lifecycle_state = 'ready' AND i.routing_mode = 'none' AND NOT i.simulated ORDER BY c.key LIMIT 1"
)


class UnroutedElastic(Action):
    @guarded
    def run(self, operation_id):
        ok, rows = call(self.config, SQL, ())
        if not ok:
            return False, rows
        if not rows:
            return True, {"found": False}
        key, generation, provider, resource_id = rows[0]
        return True, {"found": True, "slot_key": key, "slot_generation": generation, "provider": provider, "resource_id": resource_id or ""}
