"""A restart found this attempt already recorded (start_attempt created=false): decide from the provider's tags, never by
creating again (docs/07, docs/04 reconciliation). result:
  ready    the attempt had already finished ready
  failed   the attempt had already failed (cleanup was in progress): go on to cleanup
  present  exactly one resource carries this attempt's tags: adopt it (slot_state says whether it still needs binding)
  absent   the provider lists none: the create never happened or was lost; the workflow fails the attempt and cleans up (no create here)
  unknown  the provider did not answer, several resources match, or the attempt is uncertain: hold the slot
"""
import providers
from cdr_common import guarded
from cdr_db import call
from providers import tags_for
from st2common.runners.base_action import Action


class AdoptAttempt(Action):
    @guarded
    def run(self, operation_id, provider, slot_key, attempt):
        ok, rows = call(self.config, "SELECT status FROM ops.operation_attempts WHERE operation_id = %s::uuid AND attempt = %s", (operation_id, attempt))
        if not ok:
            return False, rows
        if not rows:
            return True, {"result": "unknown", "reason": "no_attempt_row"}
        status = rows[0][0]
        if status == "ready":
            return True, {"result": "ready"}
        if status == "failed":
            return True, {"result": "failed"}
        if status != "started":
            return True, {"result": "unknown", "reason": "attempt_" + status}
        ok, srows = call(self.config, "SELECT state FROM ops.capacity_slots WHERE key = %s", (slot_key,))
        if not ok:
            return False, srows
        adapter = providers.load(self.config)
        listing = adapter.list_by_tag(provider, tags_for(slot_key, operation_id, attempt))
        if listing["state"] != "ok":
            return True, {"result": "unknown", "reason": "list_unknown"}
        found = listing["resources"]
        if not found:
            return True, {"result": "absent", "simulated": adapter.simulated}
        if len(found) > 1:
            return True, {"result": "unknown", "reason": "several_resources"}
        return True, {"result": "present", "resource_id": found[0]["resource_id"], "ip": found[0].get("ip") or "", "slot_state": srows[0][0] if srows else "", "simulated": adapter.simulated}
