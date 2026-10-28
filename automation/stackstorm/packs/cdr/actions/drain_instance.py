"""Wait for in-flight connections to drain, bounded by policy scale_in.drain_timeout_seconds (docs/04). The adapter answers
drained | busy | unknown (simulation: faults; real: HAProxy `state drain`, then `show servers conn` scur). A timeout is a result: the workflow pauses and raises
an incident, it never deletes. Unknown answers are not proof of drained, so they time out too."""
import time

import providers
from cdr_common import guarded, policy_section
from lease_keeper import LeaseKeeper
from routing_bind import bind
from st2common.runners.base_action import Action


class DrainInstance(Action):
    @guarded
    def run(self, operation_id, lease_generation, provider, resource_id, slot_key='', timeout_seconds=None, poll_seconds=2):
        adapter = bind(self.config, providers.load(self.config), operation_id, lease_generation)
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
                state = adapter.drain(provider, resource_id) if adapter.simulated else adapter.drain(provider, resource_id, slot_key or None)
                if state == "drained":
                    return True, {"result": "drained", "simulated": adapter.simulated}
                if time.monotonic() >= end:
                    return True, {"result": "timeout", "last_state": state, "simulated": adapter.simulated}
                time.sleep(poll_seconds)
