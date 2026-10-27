"""Resolve the open incident of a fingerprint (ops/0012); resolved=false means none was open."""
from cdr_db import call
from st2common.runners.base_action import Action


class ResolveIncident(Action):
    def run(self, fingerprint, message, operation_id=None):
        ok, rows = call(self.config, "SELECT ops.resolve_incident(%s, %s, %s::uuid)", (fingerprint, message, operation_id))
        return (True, {"resolved": bool(rows[0][0])}) if ok else (False, rows)
