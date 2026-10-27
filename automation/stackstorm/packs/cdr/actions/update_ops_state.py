"""Project an operation's status/step and append its event (docs/05 transitions, docs/06 events).

The SQL function enforces the transition table and the event-type list; the ops API streams the rows as SSE.
"""
import json

from cdr_db import call
from st2common.runners.base_action import Action


class UpdateOpsState(Action):
    def run(self, operation_id, event_type, step, status=None, message=None, outcome=None, source="automation", execution_id=None, adapter_mode=None):
        ok, rows = call(
            self.config,
            "SELECT ops.advance_operation(%s::uuid, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)",
            (operation_id, status, step, event_type, message, None if outcome is None else json.dumps(outcome), source, execution_id, adapter_mode),
        )
        return (True, {"sequence": rows[0][0]}) if ok else (False, rows)
