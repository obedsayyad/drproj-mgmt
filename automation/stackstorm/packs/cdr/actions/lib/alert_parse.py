"""Alertmanager alert parsing shared by cdr.record_alert and cdr.alert_intent."""
import re

FINGERPRINT = re.compile(r"^[0-9a-f]{1,64}$")


def parse(alert):
    """(fingerprint, policy_version, alertname) for a recordable alert, else None."""
    if not isinstance(alert, dict) or alert.get("status") != "firing":
        return None
    fingerprint = alert.get("fingerprint")
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    version = labels.get("policy_version")
    if not isinstance(fingerprint, str) or not FINGERPRINT.match(fingerprint):
        return None
    try:
        version = int(version)
    except (TypeError, ValueError):
        return None
    if version < 1:
        return None
    name = labels.get("alertname")
    return fingerprint, version, name[:100] if isinstance(name, str) else None
