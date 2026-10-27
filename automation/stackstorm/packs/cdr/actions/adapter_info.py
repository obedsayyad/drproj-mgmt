"""Which provider adapter is configured. Workflows record it as the operation's adapter_mode and pass `simulated`
to reserve_slot, so simulated inventory can never look real. Fails when provider_adapter is unset (no default)."""
import providers
from cdr_common import guarded
from st2common.runners.base_action import Action


class AdapterInfo(Action):
    @guarded
    def run(self):
        adapter = providers.load(self.config)
        return True, {"adapter_mode": adapter.mode, "simulated": adapter.simulated}
