"""Apply a routing change. SIMULATED in P4: the database projection and the simulation adapter's routing map change; HAProxy
is not touched (the runtime-API implementation is P6, and the real adapter reports not_implemented).

target is 'primary' (modes: maint, normal), 'warm' (the bound warm-role standby: `warm`, else an interim-warm slot; none bound is
applied=false, not an error) or a slot key (modes: none, backup, active, draining). The projection is written
first (it validates readiness under the lease), then assert_lease runs immediately before the adapter call.
"""
import providers
from cdr_common import advance, guarded
from cdr_db import call
from lease_keeper import fenced
from st2common.runners.base_action import Action


class ApplyRouting(Action):
    @guarded
    def run(self, operation_id, lease_generation, target, mode, slot_generation=0, weight=None):
        adapter = providers.load(self.config)
        if target == "primary":
            if mode not in ("maint", "normal"):
                return False, "bad_argument"
        else:
            if mode not in ("none", "backup", "active", "draining"):
                return False, "bad_argument"
            if target == "warm":
                ok, rows = call(self.config, "SELECT key, generation FROM ops.capacity_slots WHERE role = 'warm' AND state = 'bound' ORDER BY (key = 'warm') DESC, key LIMIT 1", ())
                if not ok:
                    return False, rows
                if not rows:
                    return True, {"applied": False, "target": "warm", "mode": mode, "reason": "no_warm_standby", "simulated": adapter.simulated}
                target, slot_generation = rows[0]
            ok, out = call(self.config, "SELECT ops.set_routing(%s::uuid, %s, %s, %s, %s, %s)", (operation_id, lease_generation, target, slot_generation, mode, weight))
            if not ok:
                return False, out
        fenced(self.config, operation_id, lease_generation, lambda: adapter.set_routing(target, mode))
        ok, out = advance(self.config, operation_id, "operation.step.completed", f"routing_{target}_{mode}",
                          message=f"routing {target} -> {mode}" + (" (simulated)" if adapter.simulated else ""))
        if not ok:
            return False, out
        return True, {"applied": True, "target": target, "mode": mode, "simulated": adapter.simulated}
