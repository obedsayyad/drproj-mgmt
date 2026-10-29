"""Overload-only recovery observation (docs/04, T26). After the warm standby went back to backup (primary alone serving), watch
`observe_seconds` (default 60): the primary is probed directly every poll and the serving-fleet CPU (the CdrCpuHigh expression,
lib/promql.q_fleet_cpu) must stay under scale_out.cpu_percent. A failed probe is an immediate regression; CPU is judged on the last
poll's reading (30 s rate window) so the first seconds after the routing change do not count. No data is a regression (never a pass).
Returns regressed=true|false with the reason. Real adapter only."""
import time

import providers
from cdr_common import guarded, policy_section
from failback_lib import primary_ip, probe
from lease_keeper import LeaseKeeper
from promql import PromError, instant, q_fleet_cpu
from st2common.runners.base_action import Action

SETTLE_SECONDS = 30  # CPU readings before this are still the overflow period (30 s rate window)


class OverflowObserve(Action):
    @guarded
    def run(self, operation_id, lease_generation, observe_seconds=60, poll_seconds=5):
        adapter = providers.load(self.config)
        if adapter.simulated:
            raise NotImplementedError("not_implemented")
        url = self.config.get("prometheus_url")
        if not url:
            return False, "prometheus_url_missing"
        ok, so = policy_section(self.config, operation_id, "scale_out")
        if not ok:
            return False, so
        ok, coordination = policy_section(self.config, operation_id, "coordination")
        if not ok:
            return False, coordination
        ok, ip = primary_ip(self.config)
        if not ok:
            return False, ip
        started = time.monotonic()
        end = started + observe_seconds
        last_cpu = None
        with LeaseKeeper(self.config, operation_id, lease_generation, coordination["lease_renewal_seconds"]) as keeper:
            while time.monotonic() < end:
                keeper.check()
                if not all(probe(ip, adapter.ca_bundle)):
                    return True, {"regressed": True, "reason": "primary_health", "adapter_mode": adapter.mode}
                try:
                    last_cpu = instant(url, q_fleet_cpu())
                except PromError as exc:
                    return True, {"regressed": True, "reason": str(exc), "adapter_mode": adapter.mode}
                if time.monotonic() - started >= SETTLE_SECONDS and last_cpu is not None and last_cpu > so["cpu_percent"]:
                    return True, {"regressed": True, "reason": "cpu_high", "cpu_percent": round(last_cpu, 1), "adapter_mode": adapter.mode}
                time.sleep(min(poll_seconds, max(0.0, end - time.monotonic())))
        if last_cpu is None:
            return True, {"regressed": True, "reason": "no_data", "adapter_mode": adapter.mode}
        return True, {"regressed": False, "cpu_percent": round(last_cpu, 1), "observed_seconds": observe_seconds, "adapter_mode": adapter.mode}
