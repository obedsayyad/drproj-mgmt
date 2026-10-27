"""Delete the VM of one slot and prove it gone (scale-in, docs/04 steps 8-9). Scope: tags project=cdr + slot=<key>, and
nothing but the resource id recorded for the slot's instance; another tagged resource in that slot stops the delete
(result unknown) instead of being removed. assert_lease runs immediately before the delete (lib/cleanup.py)."""
import providers
from cleanup import delete_and_confirm
from cdr_common import guarded, policy_section
from lease_keeper import LeaseKeeper
from providers.base import PROJECT_TAG
from st2common.runners.base_action import Action


class DeleteSlotResource(Action):
    @guarded
    def run(self, operation_id, lease_generation, provider, slot_key, resource_id, confirm_poll_seconds=2):
        if not resource_id:
            return True, {"result": "unknown", "reason": "no_resource_id"}
        adapter = providers.load(self.config)
        ok, coordination = policy_section(self.config, operation_id, "coordination")
        if not ok:
            return False, coordination
        tags = {**PROJECT_TAG, "slot": slot_key}
        with LeaseKeeper(self.config, operation_id, lease_generation, coordination["lease_renewal_seconds"]) as keeper:
            return True, delete_and_confirm(self.config, adapter, operation_id, lease_generation, provider, tags, keeper,
                                            resource_id=resource_id, confirm_poll_seconds=confirm_poll_seconds, only_ids={resource_id})
