"""A failure-triggered intent preempts queued intents of another kind (docs/07): queued scale-ins become succeeded no-ops."""
from cdr_db import call
from st2common.runners.base_action import Action


class SupersedeQueued(Action):
    def run(self, operation_id, kind="scale_in"):
        ok, rows = call(self.config, "SELECT ops.supersede_queued(%s, %s::uuid)", (kind, operation_id))
        return (True, {"superseded": rows[0][0] or []}) if ok else (False, rows)
