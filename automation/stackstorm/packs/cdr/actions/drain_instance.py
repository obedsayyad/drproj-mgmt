"""Wait for in-flight connections to drain, bounded by policy scale_in.drain_timeout_seconds (docs/04). SIMULATED in P4
(the adapter answers drained | busy | unknown); the real drain is P6. A timeout is a result: the workflow pauses and raises
an incident, it never deletes. Unknown answers are not proof of drained, so they time out too."""
import time

import providers
from cdr_common import guarded, policy_section
from lease_keeper import LeaseKeeper
from st2common.runners.base_action import Action


class DrainInstance(Action):
    @guarded
    def run(self, operation_id, lease_generation, provider, resource_id, timeout_seconds=None, poll_seconds=2):
        adapter = providers.load(self.config)
        ok, si = policy_section(self.config, operation_id, "scale_in")
        if not ok:
            return False, si
        ok, coordination = policy_section(self.config, operation_id, "coordination")
        if not ok:
            return False, coordination
        end = time.monotonic() + (timeout_seconds if timeout_seconds is not None else si["drain_timeout_seconds"])
        with LeaseKeeper(self.config, operation_id, lease_generation, coordination["lease_renewal_seconds"]) as keeper:
            while True:
                keeper.check()
                state = adapter.drain(provider, resource_id)
                if state == "drained":
                    return True, {"result": "drained", "simulated": adapter.simulated}
                if time.monotonic() >= end:
                    return True, {"result": "timeout", "last_state": state, "simulated": adapter.simulated}
                time.sleep(poll_seconds)
