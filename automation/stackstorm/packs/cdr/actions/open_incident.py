"""Upsert the open incident for a fingerprint and append an incident_events row (ops/0011)."""
from cdr_db import call
from st2common.runners.base_action import Action


class OpenIncident(Action):
    def run(self, fingerprint, severity, source, description, resources=None, operation_id=None):
        ok, rows = call(
            self.config,
            "SELECT incident_id::text, created FROM ops.open_incident(%s, %s, %s, %s, %s::text[], %s::uuid)",
            (fingerprint, severity, source, description, resources or [], operation_id),
        )
        return (True, {"incident_id": rows[0][0], "created": rows[0][1]}) if ok else (False, rows)
