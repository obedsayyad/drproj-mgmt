"""releasing -> free, only with provider-API evidence that the resource is absent (never from a timeout)."""
import json

from cdr_db import call
from st2common.runners.base_action import Action


class ReleaseSlot(Action):
    def run(self, operation_id, lease_generation, slot_key, slot_generation, evidence):
        ok, out = call(
            self.config,
            "SELECT ops.release_slot(%s::uuid, %s, %s, %s, %s::jsonb)",
            (operation_id, lease_generation, slot_key, slot_generation, json.dumps(evidence)),
        )
        return (True, {"released": True}) if ok else (False, out)
