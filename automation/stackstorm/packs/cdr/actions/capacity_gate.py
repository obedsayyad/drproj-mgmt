"""Recheck after the lock (docs/04): maintenance, cooldown, fresh metrics. proceed=false carries the noop_reason the
workflow records on a succeeded no-op. Stale or missing control state never authorizes scaling.

Metrics freshness: lib/metrics.py (the metrics.stale alert flag, Prometheus targets, or the simulation flag).
"""
import providers
from cdr_common import guarded, policy_section
from cdr_db import call
from metrics import metrics_state
from st2common.runners.base_action import Action


class CapacityGate(Action):
    @guarded
    def run(self, operation_id):
        adapter = providers.load(self.config)
        ok, rows = call(self.config, "SELECT maintenance FROM ops.runtime_flags WHERE singleton", ())
        if not ok:
            return False, rows
        if not rows or rows[0][0]:
            return True, {"proceed": False, "noop_reason": "maintenance"}
        ok, so = policy_section(self.config, operation_id, "scale_out")
        if not ok:
            return False, so
        ok, rows = call(
            self.config,
            "SELECT count(*) FROM ops.operations WHERE kind IN ('scale_out', 'activate_standby') AND status = 'succeeded' AND id <> %s::uuid"
            " AND coalesce(outcome ->> 'noop_reason', '') = '' AND finished_at > now() - make_interval(secs => %s)",
            (operation_id, so["cooldown_seconds"]),
        )
        if not ok:
            return False, rows
        if rows[0][0]:
            return True, {"proceed": False, "noop_reason": "cooldown"}
        ok, mon = policy_section(self.config, operation_id, "monitoring")
        if not ok:
            return False, mon
        ok, m = metrics_state(self.config, adapter, mon["stale_after_seconds"])
        if not ok:
            return False, m
        if not m["fresh"]:
            return True, {"proceed": False, "noop_reason": "metrics_stale", "metrics_source": m["source"]}
        return True, {"proceed": True, "metrics_source": m["source"]}
