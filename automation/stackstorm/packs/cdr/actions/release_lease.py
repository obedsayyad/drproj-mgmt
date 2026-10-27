"""Release the capacity lease. A fenced-out worker gets released=false, not an error."""
from cdr_db import call
from st2common.runners.base_action import Action


class ReleaseLease(Action):
    def run(self, operation_id, generation, app_key="relief"):
        ok, rows = call(self.config, "SELECT ops.release_lease(%s::uuid, %s, %s)", (operation_id, generation, app_key))
        return (True, {"released": bool(rows[0][0])}) if ok else (False, rows)
