"""Create one tagged VM through the SIMULATION adapter (docs/07). assert_lease runs inside this action immediately
before the external call; the attempt row was recorded earlier by start_attempt. A LeaseKeeper renews the lease for the call.
The real adapter creates only through cdr.run_heavy (terraform_apply): here it is refused, so Terraform has one entry point.

timeout_ambiguous / unknown / unavailable are RESULTS: a timed-out create does not prove the VM does not exist,
so the workflow holds the slot instead of retrying (docs/CLAUDE.md invariants).
"""
import providers
from cdr_common import guarded, policy_section
from lease_keeper import LeaseKeeper, fenced
from providers import tags_for
from st2common.runners.base_action import Action


class ProviderCreate(Action):
    @guarded
    def run(self, operation_id, lease_generation, provider, slot_key, attempt):
        adapter = providers.load(self.config)
        if not adapter.simulated:
            return False, "terraform_via_run_heavy_only"
        ok, coordination = policy_section(self.config, operation_id, "coordination")
        if not ok:
            return False, coordination
        tags = tags_for(slot_key, operation_id, attempt)
        with LeaseKeeper(self.config, operation_id, lease_generation, coordination["lease_renewal_seconds"]) as keeper:
            res = fenced(self.config, operation_id, lease_generation, lambda: adapter.create(provider, slot_key, operation_id, attempt, tags, {}), keeper)
        return True, {**res, "simulated": adapter.simulated}
