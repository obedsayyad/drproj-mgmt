"""Poll readiness until ready, a checkpoint, or the deadline (docs/07: checkpoints from the captured policy).

Emits operation.waiting at each policy checkpoint (status waiting), admits early on the first ready answer that
carries readiness + image digest + identity, and renews the lease from a helper thread (lease_keeper). Result:
ready | deadline | unknown (the provider never answered, so non-readiness is unproven) | lease_lost.
"""
import time
from datetime import datetime, timezone

import providers
from cdr_common import advance, guarded, policy_section
from lease_keeper import LeaseKeeper
from st2common.runners.base_action import Action


def _ts(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _now():
    return datetime.now(timezone.utc)


class WaitReady(Action):
    @guarded
    def run(self, operation_id, lease_generation, provider, resource_id, deadline_at, checkpoints_at, poll_seconds=5):
        adapter = providers.load(self.config)
        ok, coordination = policy_section(self.config, operation_id, "coordination")
        if not ok:
            return False, coordination
        deadline = _ts(deadline_at)
        checkpoints = [_ts(c) for c in checkpoints_at]
        emitted = 0
        last = "not_ready"
        with LeaseKeeper(self.config, operation_id, lease_generation, coordination["lease_renewal_seconds"]) as keeper:
            while True:
                keeper.check()
                check = adapter.check_ready(provider, resource_id)
                last = check["state"]
                if last == "ready" and check.get("image_digest") and check.get("identity"):
                    return True, {"result": "ready", "image_digest": check["image_digest"], "identity": check["identity"], "checkpoints_emitted": emitted}
                while emitted < len(checkpoints) and _now() >= checkpoints[emitted]:
                    emitted += 1
                    ok, out = advance(self.config, operation_id, "operation.waiting", "wait_ready", "waiting", f"readiness checkpoint {emitted}/{len(checkpoints)}: not ready yet")
                    if not ok:
                        return False, out
                if _now() >= deadline:
                    return True, {"result": "unknown" if last == "unknown" else "deadline", "checkpoints_emitted": emitted}
                time.sleep(poll_seconds)
