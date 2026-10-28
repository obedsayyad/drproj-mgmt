"""Record the pre-failure app p95 baseline on the open primary-failure incident (docs/03, docs/04): median of the p95 over the
10 minutes before the failure (offset 1 m, the detection lag). Best effort and never fatal: no Prometheus, no data, or no open
incident means recorded=false and the failback p95 gate fails closed. Simulation records nothing (no real metrics exist)."""
import providers
from cdr_common import guarded
from cdr_db import call
from failback_lib import INCIDENT
from promql import PromError, instant, q_baseline
from st2common.runners.base_action import Action


class CaptureBaseline(Action):
    @guarded
    def run(self, operation_id):
        adapter = providers.load(self.config)
        url = self.config.get("prometheus_url")
        if adapter.simulated or not url:
            return True, {"recorded": False, "reason": "simulation" if adapter.simulated else "prometheus_url_missing"}
        try:
            value = instant(url, q_baseline())
        except PromError as exc:
            return True, {"recorded": False, "reason": str(exc)}
        if value is None or value <= 0:
            return True, {"recorded": False, "reason": "no_data"}
        ok, rows = call(self.config, "SELECT ops.set_incident_baseline(%s, %s)", (INCIDENT, value))
        if not ok:
            return True, {"recorded": False, "reason": rows}
        return True, {"recorded": bool(rows[0][0]), "baseline_p95_seconds": value}
