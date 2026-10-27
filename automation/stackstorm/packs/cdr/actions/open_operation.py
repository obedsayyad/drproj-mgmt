"""Open (or return) the operation for an automation-originated intent; idempotent on the fingerprint and captures the active policy version (ops/0011)."""
from cdr_db import call
from st2common.runners.base_action import Action


class OpenOperation(Action):
    def run(self, kind, source, fingerprint, event_name=None):
        ok, rows = call(self.config, "SELECT operation_id::text, created, policy_version FROM ops.open_operation(%s, %s, %s, %s)", (kind, source, fingerprint, event_name))
        if not ok:
            return False, rows
        op, created, version = rows[0]
        return True, {"operation_id": op, "created": created, "policy_version": version}
