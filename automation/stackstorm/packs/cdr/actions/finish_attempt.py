"""Move an attempt through started -> ready|failed|uncertain -> cleaned (transitions enforced by the database)."""
import json

from cdr_db import call
from st2common.runners.base_action import Action


class FinishAttempt(Action):
    def run(self, operation_id, lease_generation, attempt, status, error_code=None, result_refs=None):
        ok, out = call(
            self.config,
            "SELECT ops.finish_attempt(%s::uuid, %s, %s, %s, %s, %s::jsonb)",
            (operation_id, lease_generation, attempt, status, error_code, None if result_refs is None else json.dumps(result_refs)),
        )
        return (True, {"status": status}) if ok else (False, out)
