"""SIMULATION producer of `standby.unhealthy` (docs/04 warm standby repair; the real producer is HAProxy health, P6).

Refuses unless the provider adapter is the simulation, so it can never be mistaken for a real detection. `sim_fault` first breaks the
warm-role standby inside the simulation (app_broken | powered_off | absent), then a standby_repair operation is opened (idempotent per
slot generation and second) and dispatched to StackStorm. Nothing here repairs anything; the workflow decides from provider answers."""
import time

import providers
from cdr_common import advance, guarded
from cdr_db import call
from st2_api import St2Api, St2Unavailable
from st2common.runners.base_action import Action

FAULT_FIELDS = {"app_broken": {"app_broken": True}, "powered_off": {"power": "off"}, "absent": {"deleted": True}}


class StandbyUnhealthy(Action):
    def _api(self):
        return St2Api()

    @guarded
    def run(self, sim_fault="none", app_key="relief"):
        adapter = providers.load(self.config)
        if not adapter.simulated:
            return False, "not_simulation"
        ok, rows = call(
            self.config,
            "SELECT s.key, s.generation, i.provider_resource_id FROM ops.capacity_slots s LEFT JOIN ops.instances i ON i.id = s.bound_instance_id"
            " WHERE s.key = 'warm'",
            (),
        )
        if not ok:
            return False, rows
        key, generation, rid = rows[0]
        if sim_fault != "none":
            if not rid or not adapter.set_resource(rid, **FAULT_FIELDS[sim_fault]):
                return False, "no_warm_resource_to_break"
        ok, rows = call(self.config, "SELECT operation_id::text, created FROM ops.open_operation('standby_repair', 'automation', %s, 'standby.unhealthy')",
                        (f"standby.unhealthy:{key}:{generation}:{int(time.time())}",))
        if not ok:
            return False, rows
        op, created = rows[0]
        out = {"operation_id": op, "created": created, "sim_fault": sim_fault, "simulated": True}
        if created:
            ok, seq = advance(self.config, op, "standby.unhealthy", "trigger", message=f"standby.unhealthy (simulated producer, {sim_fault})")
            if not ok:
                return False, seq
            try:
                out["execution_id"] = self._api().run("cdr.standby_repair", {"operation_id": op})
            except St2Unavailable as exc:
                out["dispatch_error"] = str(exc)  # the operation stays queued; reconcile closes it as stale_intent
        return True, out
