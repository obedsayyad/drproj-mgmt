"""Failback step 3 (docs/04): after `state ready`, wait for HAProxy to report the primary UP (health check rise 6, about 60 s)."""
import time

import providers
from cdr_common import guarded, policy_section
from haproxy_admin import AdminError
from lease_keeper import LeaseKeeper
from st2common.runners.base_action import Action


class FailbackWaitUp(Action):
    @guarded
    def run(self, operation_id, lease_generation, timeout_seconds=180, poll_seconds=5):
        adapter = providers.load(self.config)
        if adapter.simulated:
            raise NotImplementedError("not_implemented")
        ok, coordination = policy_section(self.config, operation_id, "coordination")
        if not ok:
            return False, coordination
        end = time.monotonic() + timeout_seconds
        with LeaseKeeper(self.config, operation_id, lease_generation, coordination["lease_renewal_seconds"]) as keeper:
            while time.monotonic() < end:
                keeper.check()
                try:
                    if adapter.membership().get("primary").is_up:
                        return True, {"up": True, "adapter_mode": adapter.mode}
                except AdminError:
                    pass  # unknown answer is not UP; keep polling until the timeout
                time.sleep(poll_seconds)
        return True, {"up": False, "adapter_mode": adapter.mode}
