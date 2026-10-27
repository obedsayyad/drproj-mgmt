"""Power on a powered-off warm standby (docs/04 repair class b). assert_lease runs inside this action immediately before the call; ok / unknown /
unavailable are results (a timeout is not proof either way), the workflow decides."""
import providers
from cdr_common import guarded
from lease_keeper import fenced
from st2common.runners.base_action import Action


class ProviderPowerOn(Action):
    @guarded
    def run(self, operation_id, lease_generation, provider, resource_id):
        adapter = providers.load(self.config)
        res = fenced(self.config, operation_id, lease_generation, lambda: adapter.power_on(provider, resource_id))
        return True, {**res, "simulated": adapter.simulated}
