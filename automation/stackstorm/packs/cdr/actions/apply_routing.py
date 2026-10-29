"""Apply a routing change.

Simulation adapter: the database projection and the simulation adapter's routing map change; HAProxy is not touched.
Real adapter (adapter_mode real): the change is applied on the HAProxy be_app server lines through the Runtime API (lib/haproxy_admin.py,
lib/haproxy_routing.py). Each command is preceded by assert_lease and followed by a membership dump and an ops audit record.

target is 'lb' (modes normal | overflow | ramp (step_percent) | primary_failure), 'primary' (modes: maint, normal), 'warm' (the bound
warm-role standby: `warm`, else an interim-warm slot; none bound is applied=false, not an error) or a slot key (modes: none, backup,
active, draining). ops.check_routing validates readiness under the lease first (no write); the adapter then sends the commands (each preceded by
assert_lease + a pending ops.haproxy_commands row); ops.set_routing, the projection, is the completion step.
"""
import providers
from cdr_common import advance, guarded
from cdr_db import call
from lease_keeper import fenced
from routing_bind import bind, serving_lines, slot_ip
from st2common.runners.base_action import Action

LB_MODES = ("normal", "overflow", "ramp", "primary_failure")


class ApplyRouting(Action):
    @guarded
    def run(self, operation_id, lease_generation, target, mode, slot_generation=0, weight=None, step_percent=None):
        adapter = providers.load(self.config)
        extra = {}
        if target == "lb":
            if mode not in LB_MODES or (mode == "ramp" and step_percent is None):
                return False, "bad_argument"
            if mode == "ramp":
                extra["step_percent"] = step_percent
            if not adapter.simulated:
                ok, serving = serving_lines(self.config)
                if not ok:
                    return False, serving
                extra["serving"] = serving
        elif target == "primary":
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
            # Validation under the lease without committing: the projection must not claim a routing the LB never applied.
            ok, out = call(self.config, "SELECT ops.check_routing(%s::uuid, %s, %s, %s, %s, %s)", (operation_id, lease_generation, target, slot_generation, mode, weight))
            if not ok:
                return False, out
            if not adapter.simulated and (mode == "backup" or (mode == "active" and not target.startswith("primary"))):
                ok, ip = slot_ip(self.config, target)
                if not ok:
                    return False, ip
                extra["ip"] = ip
        bind(self.config, adapter, operation_id, lease_generation)
        if adapter.simulated:  # no per-command intent rows: assert_lease right before the simulated call
            fenced(self.config, operation_id, lease_generation, lambda: adapter.set_routing(target, mode, **extra))
        else:  # the adapter asserts the lease and writes the pending intent before every command it sends
            adapter.set_routing(target, mode, **extra)
        if target not in ("lb", "primary"):  # completion step: the projection follows the applied HAProxy change
            ok, out = call(self.config, "SELECT ops.set_routing(%s::uuid, %s, %s, %s, %s, %s)", (operation_id, lease_generation, target, slot_generation, mode, weight))
            if not ok:
                return False, out
        ok, out = advance(self.config, operation_id, "operation.step.completed", f"routing_{target}_{mode}",
                          message=f"routing {target} -> {mode}" + (" (simulated)" if adapter.simulated else " (real)"))
        if not ok:
            return False, out
        return True, {"applied": True, "target": target, "mode": mode, "simulated": adapter.simulated, "adapter_mode": adapter.mode}
