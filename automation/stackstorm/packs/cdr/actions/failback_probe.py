"""Failback step 1 (docs/04): direct probes of the primary from the management VM (:8443 /health/live and /health/ready) must be healthy
for policy failback.stable_seconds continuously. Any failed probe restarts the clock; never stable within 5x that window -> stable=false.
Real adapter only: the simulation has no primary to probe, so it reports not_implemented."""
import time

import providers
from cdr_common import guarded, policy_section
from failback_lib import primary_ip, probe
from lease_keeper import LeaseKeeper
from st2common.runners.base_action import Action


class FailbackProbe(Action):
    @guarded
    def run(self, operation_id, lease_generation, poll_seconds=5):
        adapter = providers.load(self.config)
        if adapter.simulated:
            raise NotImplementedError("not_implemented")
        ok, fb = policy_section(self.config, operation_id, "failback")
        if not ok:
            return False, fb
        ok, coordination = policy_section(self.config, operation_id, "coordination")
        if not ok:
            return False, coordination
        ok, ip = primary_ip(self.config)
        if not ok:
            return False, ip
        need = fb["stable_seconds"]
        give_up = time.monotonic() + 5 * need
        since = None
        with LeaseKeeper(self.config, operation_id, lease_generation, coordination["lease_renewal_seconds"]) as keeper:
            while time.monotonic() < give_up:
                keeper.check()
                if all(probe(ip, adapter.ca_bundle)):
                    since = since or time.monotonic()
                    if time.monotonic() - since >= need:
                        return True, {"stable": True, "stable_seconds": need, "adapter_mode": adapter.mode}
                else:
                    since = None
                time.sleep(poll_seconds)
        return True, {"stable": False, "adapter_mode": adapter.mode}
