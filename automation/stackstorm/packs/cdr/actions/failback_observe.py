"""Failback ramp observation (docs/04). After the routing change for `step_percent` (cdr.apply_routing target lb), observe the step for
policy failback.step_observation_seconds (final step: final_step_observation_seconds), probing the primary directly every poll.
A failed direct probe is an immediate regression. At the end the gates are read from Prometheus (lib/promql.py):
  2 consecutive direct health successes, samples after the step started, 5xx < gate_max_5xx_ratio, p95 < gate_max_p95_ratio_to_baseline x baseline
  (baseline stored on the incident), primary CPU < gate_max_primary_cpu_percent. Missing data or a missing baseline fails the gate (never passes).
Returns passed=true | passed=false with the failing gates. Real adapter only (simulation reports not_implemented)."""
import time

import providers
from cdr_common import guarded, policy_section
from failback_lib import baseline_p95, primary_ip, probe
from lease_keeper import LeaseKeeper
from promql import PromError, instant, judge, q_5xx_ratio, q_cpu, q_fresh, q_p95
from st2common.runners.base_action import Action


class FailbackObserve(Action):
    @guarded
    def run(self, operation_id, lease_generation, step_percent, poll_seconds=10):
        adapter = providers.load(self.config)
        if adapter.simulated:
            raise NotImplementedError("not_implemented")
        url = self.config.get("prometheus_url")
        if not url:
            return False, "prometheus_url_missing"
        ok, fb = policy_section(self.config, operation_id, "failback")
        if not ok:
            return False, fb
        ok, coordination = policy_section(self.config, operation_id, "coordination")
        if not ok:
            return False, coordination
        ok, ip = primary_ip(self.config)
        if not ok:
            return False, ip
        ok, baseline = baseline_p95(self.config)
        if not ok:
            return False, baseline
        steps = fb["primary_traffic_steps_percent"]
        observe = fb["final_step_observation_seconds"] if step_percent == steps[-1] else fb["step_observation_seconds"]
        started = time.time()
        end = time.monotonic() + observe
        streak = 0
        with LeaseKeeper(self.config, operation_id, lease_generation, coordination["lease_renewal_seconds"]) as keeper:
            while time.monotonic() < end:
                keeper.check()
                result = probe(ip, adapter.ca_bundle)
                if all(result):
                    streak += 1
                else:
                    return True, {"passed": False, "failed_gates": ["health"], "probe_failure": list(getattr(result, "detail", ())), "after_good_probes": streak, "step_percent": step_percent, "adapter_mode": adapter.mode}
                time.sleep(min(poll_seconds, max(0.0, end - time.monotonic())))
        try:
            values = {"5xx": instant(url, q_5xx_ratio()), "p95": instant(url, q_p95()), "cpu": instant(url, q_cpu(ip)), "fresh": instant(url, q_fresh())}
        except PromError as exc:
            return True, {"passed": False, "failed_gates": [str(exc)], "step_percent": step_percent, "adapter_mode": adapter.mode}
        gates = judge(values, baseline, fb, started)
        gates["health"] = {"ok": streak >= fb["gate_consecutive_health_successes"], "value": streak}
        gates["baseline"] = {"ok": baseline is not None, "value": baseline}
        failed = sorted(k for k, g in gates.items() if not g["ok"])
        return True, {"passed": not failed, "failed_gates": failed, "gates": gates, "step_percent": step_percent, "adapter_mode": adapter.mode}
