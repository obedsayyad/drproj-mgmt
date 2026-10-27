"""bound|reserved -> releasing. Starts deletion bookkeeping only; the slot stays counted until release_slot."""
from cdr_db import call
from st2common.runners.base_action import Action


class BeginSlotRelease(Action):
    def run(self, operation_id, lease_generation, slot_key, slot_generation):
        ok, out = call(self.config, "SELECT ops.begin_slot_release(%s::uuid, %s, %s, %s)", (operation_id, lease_generation, slot_key, slot_generation))
        return (True, {"releasing": True}) if ok else (False, out)
