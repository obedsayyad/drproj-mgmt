"""Record Alertmanager notifications (docs/06, docs/07). Recording only: no capacity or infrastructure action.

Only firing alerts with a hex fingerprint and an integer policy_version label are stored; resolved or
malformed alerts are counted and skipped, never invented. Repeated notifications for the same
(fingerprint, policy_version) coalesce into one row and bump last_seen_at. Only short error classes are
returned, because driver messages can contain connection strings.
"""
import psycopg
from alert_parse import FINGERPRINT, parse  # noqa: F401 - parse is re-exported
from st2common.runners.base_action import Action

CONNECT_TIMEOUT_SECONDS = 10
MAX_ALERTS = 200  # Alertmanager groups are small; refuse absurd payloads instead of looping over them


def classify(exc):
    if isinstance(exc, psycopg.OperationalError):
        return "connect_or_network"
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return "privilege"
    return type(exc).__name__[:60]


class RecordAlert(Action):
    def run(self, body):
        url = self.config.get("supabase_ops_url")
        if not url:
            return False, "config_missing"
        alerts = body.get("alerts") if isinstance(body, dict) else None
        if not isinstance(alerts, list) or len(alerts) > MAX_ALERTS:
            return False, "bad_payload"
        rows = [r for r in map(parse, alerts) if r]
        try:
            with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_SECONDS, autocommit=True) as conn:
                for fingerprint, version, name in rows:
                    conn.execute(
                        "INSERT INTO ops.processed_events (fingerprint, policy_version, source, event_name)"
                        " VALUES (%s, %s, 'alertmanager', %s)"
                        " ON CONFLICT (fingerprint, policy_version)"
                        " DO UPDATE SET last_seen_at = now(), event_name = EXCLUDED.event_name",
                        (fingerprint, version, name),
                    )
        except Exception as exc:  # noqa: BLE001 - recorded by class only
            return False, "record_failed:" + classify(exc)
        return True, {"recorded": len(rows), "skipped": len(alerts) - len(rows)}
