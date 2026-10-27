"""A readiness window for redeploying an existing standby: wait_ready takes absolute times, and unlike a new attempt (start_attempt) a repair has no
attempt row to store them in. Checkpoints and deadline come from the captured policy (240/360/480 by default)."""
from datetime import datetime, timedelta, timezone

from cdr_common import guarded, policy_section
from st2common.runners.base_action import Action


class StandbyWindow(Action):
    @guarded
    def run(self, operation_id):
        ok, pol = policy_section(self.config, operation_id, "provisioning")
        if not ok:
            return False, pol
        now = datetime.now(timezone.utc)
        return True, {"deadline_at": (now + timedelta(seconds=pol["readiness_deadline_seconds"])).isoformat(),
                      "checkpoints_at": [(now + timedelta(seconds=s)).isoformat() for s in pol["readiness_checkpoints_seconds"]]}
