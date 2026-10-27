"""Advisory pick for the next elastic VM (alternating providers, 1 VM per step). Providers the adapter reports
unavailable or unknown are skipped; reserve_slot re-checks the cap atomically. provider=null means none qualifies."""
import providers
from cdr_common import guarded
from cdr_db import call
from st2common.runners.base_action import Action


class NextElasticProvider(Action):
    @guarded
    def run(self):
        adapter = providers.load(self.config)
        skip = [p for p in ("digitalocean", "linode") if adapter.observe(p) != "available"]
        ok, rows = call(self.config, "SELECT ops.next_elastic_provider(%s::text[])", (skip,))
        if not ok:
            return False, rows
        return True, {"provider": rows[0][0], "skipped": skip, "simulated": adapter.simulated, "adapter_mode": adapter.mode}
