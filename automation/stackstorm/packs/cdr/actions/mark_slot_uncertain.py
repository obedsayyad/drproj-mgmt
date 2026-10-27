"""Existence unknown: the slot stays counted as held_uncertain until reconcile resolves it with provider-API evidence."""
from cdr_db import call
from st2common.runners.base_action import Action


class MarkSlotUncertain(Action):
    def run(self, operation_id, lease_generation, slot_key, slot_generation, reason):
        ok, out = call(self.config, "SELECT ops.mark_slot_uncertain(%s::uuid, %s, %s, %s, %s)", (operation_id, lease_generation, slot_key, slot_generation, reason))
        return (True, {"held_uncertain": True}) if ok else (False, out)
