"""apply_policy activation (docs/03 activation 6). ops.finish_policy_apply makes the pending version active and supersedes the old one.
A cap lowered below the occupied count of a provider is a pending transition (docs/03): nothing is released or deleted; reserve_slot
already refuses new slots above the new cap, and scale-in/reconcile converge. Reported per provider in `pending_transition`."""
import json

from cdr_common import guarded, policy_versions
from cdr_db import call
from lease_keeper import fenced
from st2common.runners.base_action import Action


class PolicyActivate(Action):
    @guarded
    def run(self, operation_id, lease_generation=0):
        ok, v = policy_versions(self.config, operation_id)
        if not ok:
            return False, v
        caps = v["pending"]["capacity"]["max_cloud_vms_per_provider"]
        ok, rows = call(self.config, "SELECT provider, count(*) FROM ops.capacity_slots WHERE state <> 'free' GROUP BY provider", ())
        if not ok:
            return False, rows
        occupied = {p: n for p, n in rows}
        pending = [{"provider": p, "occupied": occupied.get(p, 0), "cap": caps[p]} for p in ("digitalocean", "linode") if occupied.get(p, 0) > caps[p]]
        result = {"activated_by": operation_id, "pending_transition": pending}
        sql = "SELECT ops.finish_policy_apply(%s::uuid, %s, true, %s::jsonb)"
        args = (operation_id, v["version"], json.dumps(result))
        if lease_generation:
            ok, out = fenced(self.config, operation_id, lease_generation, lambda: call(self.config, sql, args))
        else:
            ok, out = call(self.config, sql, args)
        if not ok:
            return False, out
        return True, {"state": out[0][0], "version": v["version"], "pending_transition": pending}
