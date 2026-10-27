"""Recheck under the lock before scale-in (docs/04): maintenance, primary outage (T22), stale metrics, remaining capacity,
candidate. proceed=false carries the noop_reason for a succeeded no-op. The candidate is the newest bound elastic slot
(ops.scale_in_candidate); the warm, interim-warm and primary are never candidates.

Primary outage = an open `primary.unhealthy:*` incident, or a non-terminal primary_failure/failback operation. Low traffic
during an outage never removes cloud redundancy.
"""
import providers
from cdr_common import guarded, policy_section
from cdr_db import call
from metrics import metrics_state
from st2common.runners.base_action import Action


class ScaleInGate(Action):
    @guarded
    def run(self, operation_id):
        adapter = providers.load(self.config)
        ok, rows = call(self.config, "SELECT maintenance FROM ops.runtime_flags WHERE singleton", ())
        if not ok:
            return False, rows
        if not rows or rows[0][0]:
            return True, {"proceed": False, "noop_reason": "maintenance"}
        ok, rows = call(
            self.config,
            "SELECT (SELECT count(*) FROM ops.incidents WHERE state = 'open' AND fingerprint LIKE 'primary.unhealthy%%')"
            " + (SELECT count(*) FROM ops.operations WHERE kind IN ('primary_failure', 'failback') AND status NOT IN ('succeeded', 'failed', 'cancelled') AND id <> %s::uuid)",
            (operation_id,),
        )
        if not ok:
            return False, rows
        if rows[0][0]:
            return True, {"proceed": False, "noop_reason": "primary_outage"}
        # ANY other scale_in that has started and is not terminal (paused, reconciling, ... regardless of fingerprint): a second
        # cpu.low must not start a second drain; it ends as a succeeded no-op. Queued/waiting ones have not touched anything.
        ok, rows = call(
            self.config,
            "SELECT count(*) FROM ops.operations WHERE kind = 'scale_in' AND status IN ('running', 'verifying', 'paused', 'reconciling', 'cancelling')"
            " AND id <> %s::uuid",
            (operation_id,),
        )
        if not ok:
            return False, rows
        if rows[0][0]:
            return True, {"proceed": False, "noop_reason": "scale_in_in_progress"}
        ok, mon = policy_section(self.config, operation_id, "monitoring")
        if not ok:
            return False, mon
        ok, m = metrics_state(self.config, adapter, mon["stale_after_seconds"])
        if not ok:
            return False, m
        if not m["fresh"]:
            return True, {"proceed": False, "noop_reason": "metrics_stale", "metrics_source": m["source"]}
        ok, rows = call(
            self.config,
            "SELECT count(*) FROM ops.capacity_slots s JOIN ops.instances i ON i.id = s.bound_instance_id"
            " WHERE s.role = 'warm' AND s.state = 'bound' AND i.lifecycle_state = 'ready'",
            (),
        )
        if not ok:
            return False, rows
        if not rows[0][0]:
            return True, {"proceed": False, "noop_reason": "warm_not_ready"}
        ok, rows = call(
            self.config,
            "SELECT c.key, c.generation, c.provider, i.provider_resource_id FROM ops.capacity_slots c JOIN ops.instances i ON i.id = c.bound_instance_id"
            " WHERE c.key = ops.scale_in_candidate()",
            (),
        )
        if not ok:
            return False, rows
        if not rows:
            return True, {"proceed": False, "noop_reason": "no_candidate"}
        key, generation, provider, resource_id = rows[0]
        return True, {"proceed": True, "slot_key": key, "slot_generation": generation, "provider": provider, "resource_id": resource_id or "", "metrics_source": m["source"]}
