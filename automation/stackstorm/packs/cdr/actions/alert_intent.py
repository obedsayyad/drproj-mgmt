"""Alertmanager notification -> recorded alert -> capacity intent (docs/07). Replaces recording-only cdr.record_alert in the
cdr_alert rule. No infrastructure is touched here: an intent is an ops operation (idempotent per alert fingerprint and
policy version, via ops.open_operation) that is dispatched to its workflow once.

  metrics.stale          firing sets, resolved clears the stale flag (blocks CPU-driven intents and scale-in)
  app.cpu.high           activate_standby while an idle warm standby is ready and activate_warm_standby_first is on, else scale_out
  app.cpu.low            scale_in
  app.primary.unhealthy  opens the primary.unhealthy:relief incident, supersedes queued scale-ins, primary_failure
  standby.unhealthy      standby_repair (HAProxy reports a warm line DOWN, P6.1); standby_classify decides the repair under the lock
Cooldown and every other recheck happen under the lock in the workflow, not here.
"""
from cdr_common import advance
from cdr_db import call
from alert_parse import parse
from st2_api import St2Api, St2Unavailable
from st2common.runners.base_action import Action

EVENT_BY_ALERT = {
    "CdrCpuHigh": "app.cpu.high",
    "CdrCpuLow": "app.cpu.low",
    "CdrPrimaryUnhealthy": "app.primary.unhealthy",
    "CdrStandbyUnhealthy": "standby.unhealthy",
    "CdrServingMemberStale": "metrics.stale",
    "CdrServingMemberMissing": "metrics.stale",
    "CdrServingTargetDown": "metrics.stale",
    "CdrMembershipAbsent": "metrics.stale",
}
INTENT_KIND = {"app.cpu.low": "scale_in", "app.primary.unhealthy": "primary_failure", "standby.unhealthy": "standby_repair"}
CPU_EVENTS = ("app.cpu.high", "app.cpu.low")
OUTAGE_SQL = (
    "SELECT EXISTS (SELECT 1 FROM ops.incidents WHERE state = 'open' AND fingerprint LIKE 'primary.unhealthy%%')"
    " OR EXISTS (SELECT 1 FROM ops.operations WHERE kind IN ('primary_failure', 'failback') AND status NOT IN ('succeeded', 'failed', 'cancelled'))"
)
# No elastic VM to remove and the warm standby is not serving as overflow (the case scale_in_gate ends as no_candidate without recover_warm).
NOTHING_TO_SCALE_IN_SQL = (
    "SELECT ops.scale_in_candidate() IS NULL AND NOT EXISTS (SELECT 1 FROM ops.capacity_slots c JOIN ops.instances i ON i.id = c.bound_instance_id"
    " WHERE c.role = 'warm' AND c.state = 'bound' AND i.lifecycle_state = 'ready' AND i.routing_mode = 'active')"
)


def event_of(alert):
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    return labels.get("cdr_event") or EVENT_BY_ALERT.get(labels.get("alertname"))


class AlertIntent(Action):
    def _api(self):
        return St2Api()

    def run(self, body):
        alerts = body.get("alerts") if isinstance(body, dict) else None
        if not isinstance(alerts, list) or len(alerts) > 200:
            return False, "bad_payload"
        recorded = 0
        for alert in alerts:
            row = parse(alert)
            if row:
                ok, out = call(
                    self.config,
                    "INSERT INTO ops.processed_events (fingerprint, policy_version, source, event_name) VALUES (%s, %s, 'alertmanager', %s)"
                    " ON CONFLICT (fingerprint, policy_version) DO UPDATE SET last_seen_at = now(), event_name = EXCLUDED.event_name RETURNING 1",
                    row,
                )
                if not ok:
                    return False, "record_failed:" + out
                recorded += 1
        firing = {}
        stale_firing = stale_resolved = False
        for alert in alerts:
            if not isinstance(alert, dict):
                continue
            event = event_of(alert)
            if event == "metrics.stale":
                stale_firing |= alert.get("status") == "firing"
                stale_resolved |= alert.get("status") == "resolved"
            elif event and alert.get("status") == "firing" and isinstance(alert.get("fingerprint"), str):
                firing.setdefault(event, alert["fingerprint"])
        if stale_firing or stale_resolved:
            ok, out = call(self.config, "SELECT ops.set_metrics_stale(%s)", (stale_firing,))
            if not ok:
                return False, out
        ok, rows = call(self.config, "SELECT metrics_stale_since IS NOT NULL FROM ops.runtime_flags WHERE singleton", ())
        if not ok:
            return False, rows
        stale = bool(rows and rows[0][0])
        cpu_low_skip = None
        if "app.cpu.low" in firing:
            ok, rows = call(self.config, OUTAGE_SQL, ())
            if not ok:
                return False, rows
            if rows and rows[0][0]:
                cpu_low_skip = "primary_outage"
            else:
                ok, rows = call(self.config, NOTHING_TO_SCALE_IN_SQL, ())
                if not ok:
                    return False, rows
                if rows and rows[0][0]:
                    cpu_low_skip = "no_candidate"
        intents = []
        for event, fingerprint in firing.items():
            if event in CPU_EVENTS and stale:
                intents.append({"event": event, "skipped": "metrics_stale"})
                continue
            if event == "app.cpu.low" and cpu_low_skip:
                # scale_in_gate would end every one of these as a noop; Alertmanager repeats the alert every 60 s and must not open an operation each time
                intents.append({"event": event, "skipped": cpu_low_skip})
                continue
            kind = self._kind(event)
            if isinstance(kind, tuple):
                return False, kind[1]
            ok, rows = call(self.config, "SELECT operation_id::text, created FROM ops.open_operation(%s, 'alertmanager', %s, %s)", (kind, f"alert:{event}:{fingerprint}", event))
            if not ok:
                return False, rows
            op, created = rows[0]
            item = {"event": event, "kind": kind, "operation_id": op, "created": created}
            if event == "app.primary.unhealthy":
                ok, out = call(self.config, "SELECT incident_id::text FROM ops.open_incident('primary.unhealthy:relief', 'critical', 'alertmanager', 'the primary failed its health checks', ARRAY['primary'], %s::uuid)", (op,))
                if not ok:
                    return False, out
                ok, out = call(self.config, "SELECT ops.supersede_queued('scale_in', %s::uuid)", (op,))
                if not ok:
                    return False, out
                item["superseded"] = out[0][0] or []
            if created:
                advance(self.config, op, event, "trigger", message=f"{event} (alert {fingerprint[:12]})", source="alertmanager")
                try:
                    item["execution_id"] = self._api().run(f"cdr.{kind}", {"operation_id": op})
                except St2Unavailable as exc:
                    item["dispatch_error"] = str(exc)  # the operation stays queued; reconcile closes it as stale_intent
            intents.append(item)
        return True, {"recorded": recorded, "stale": stale, "intents": intents}

    def _kind(self, event):
        if event in INTENT_KIND:
            return INTENT_KIND[event]
        if event != "app.cpu.high":
            return ("err", "unknown_event")
        ok, rows = call(self.config, "SELECT configuration -> 'scale_out' ->> 'activate_warm_standby_first' FROM ops.policy_versions WHERE status = 'active'", ())
        if not ok:
            return ("err", rows)
        warm_first = bool(rows) and rows[0][0] == "true"
        ok, rows = call(
            self.config,
            "SELECT count(*) FILTER (WHERE i.routing_mode <> 'active'), count(*) FILTER (WHERE i.routing_mode = 'active')"
            " FROM ops.capacity_slots s JOIN ops.instances i ON i.id = s.bound_instance_id WHERE s.role = 'warm' AND s.state = 'bound' AND i.lifecycle_state = 'ready'",
            (),
        )
        if not ok:
            return ("err", rows)
        idle, active = rows[0]
        return "activate_standby" if warm_first and idle and not active else "scale_out"
