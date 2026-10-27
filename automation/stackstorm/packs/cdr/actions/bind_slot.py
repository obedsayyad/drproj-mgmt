"""reserved -> bound once the provider resource exists."""
from cdr_db import call
from st2common.runners.base_action import Action


class BindSlot(Action):
    def run(self, operation_id, lease_generation, slot_key, slot_generation, resource_id, public_ip=None, vcpu=None, memory_mb=None):
        ok, out = call(
            self.config,
            "SELECT ops.bind_slot(%s::uuid, %s, %s, %s, %s, %s, %s, %s)",
            (operation_id, lease_generation, slot_key, slot_generation, resource_id, public_ip, vcpu, memory_mb),
        )
        return (True, {"bound": True}) if ok else (False, out)
