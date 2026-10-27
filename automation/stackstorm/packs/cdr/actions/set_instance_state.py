"""Record deploy/ready/failed and power state of the instance bound to a slot (ops/0011, fenced)."""
from cdr_db import call
from st2common.runners.base_action import Action


class SetInstanceState(Action):
    def run(self, operation_id, lease_generation, slot_key, slot_generation, lifecycle, power_state=None, image_digest=None):
        ok, out = call(
            self.config,
            "SELECT ops.set_instance_state(%s::uuid, %s, %s, %s, %s, %s, %s)",
            (operation_id, lease_generation, slot_key, slot_generation, lifecycle, power_state, image_digest),
        )
        return (True, {"lifecycle": lifecycle}) if ok else (False, out)
