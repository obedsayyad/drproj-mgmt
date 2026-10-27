"""Fencing check before every side-effecting call (docs/07): the lease must still be ours at this generation."""
from cdr_db import call
from st2common.runners.base_action import Action


class AssertLease(Action):
    def run(self, operation_id, generation, app_key="relief"):
        ok, out = call(self.config, "SELECT ops.assert_lease(%s::uuid, %s, %s)", (operation_id, generation, app_key))
        return (True, {"asserted": True}) if ok else (False, out)
