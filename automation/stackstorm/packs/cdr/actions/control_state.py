"""Control-state continuity (docs/04): when the Supabase lease store cannot be reached, nothing can be written to ops, so
StackStorm's own datastore remembers it. `down` records the first failure time; `recover` (run by the next reconcile that
can reach the database) writes control_state.unavailable and control_state.recovered events on its operation, opens and
resolves a `control_state.unavailable:<app>` incident so the outage appears in the ops records, then clears the key."""
from datetime import datetime, timezone

from cdr_common import advance
from cdr_db import call
from st2common.runners.base_action import Action

KEY = "control_state_unavailable_since"
FINGERPRINT = "control_state.unavailable:relief"


class ControlState(Action):
    def run(self, mode, operation_id=None):
        svc = self.action_service
        if mode == "down":
            if not svc.get_value(KEY):
                svc.set_value(KEY, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            return True, {"recorded": True}
        if mode != "recover" or not operation_id:
            return False, "bad_argument"
        since = svc.get_value(KEY)
        if not since:
            return True, {"was_down": False}
        for event, step, message in (
            ("control_state.unavailable", "control_state", f"the lease store was unreachable since {since}; capacity mutations were refused meanwhile"),
            ("control_state.recovered", "control_state", "the lease store is reachable again"),
        ):
            ok, out = advance(self.config, operation_id, event, step, message=message, source="reconciliation")
            if not ok:
                return False, out
        ok, rows = call(self.config, "SELECT incident_id::text FROM ops.open_incident(%s, 'warning', 'reconciliation', %s, ARRAY['control_state'], %s::uuid)",
                        (FINGERPRINT, f"lease store unreachable since {since}", operation_id))
        if not ok:
            return False, rows
        ok, rows = call(self.config, "SELECT ops.resolve_incident(%s, 'lease store reachable again', %s::uuid)", (FINGERPRINT, operation_id))
        if not ok:
            return False, rows
        svc.delete_value(KEY)
        return True, {"was_down": True, "since": since}
