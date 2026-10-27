"""Delete the resource(s) of one attempt and confirm absence through the provider API (docs/07).

Only project-tagged resources of this exact (slot, op, attempt) are touched: the tag listing is the scope, never
a bare id. result=cleaned carries evidence for release_slot; unknown / still_present mean absence is NOT proven,
so the workflow holds the slot. assert_lease runs immediately before every delete (lib/cleanup.py).
"""
import providers
from cleanup import delete_and_confirm
from cdr_common import guarded, policy_section
from lease_keeper import LeaseKeeper
from providers import tags_for
from st2common.runners.base_action import Action


class ProviderCleanup(Action):
    @guarded
    def run(self, operation_id, lease_generation, provider, slot_key, attempt, resource_id=None, confirm_poll_seconds=2):
        adapter = providers.load(self.config)
        ok, coordination = policy_section(self.config, operation_id, "coordination")
        if not ok:
            return False, coordination
        tags = tags_for(slot_key, operation_id, attempt)
        with LeaseKeeper(self.config, operation_id, lease_generation, coordination["lease_renewal_seconds"]) as keeper:
            return True, delete_and_confirm(self.config, adapter, operation_id, lease_generation, provider, tags, keeper,
                                            resource_id=resource_id, confirm_poll_seconds=confirm_poll_seconds)
