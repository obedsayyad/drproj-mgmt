"""Shared helpers of the failback actions (docs/04): primary identity, direct health probes, incident baseline."""
import requests

from cdr_db import call

INCIDENT = "primary.unhealthy:relief"


def primary_ip(config):
    ok, rows = call(config, "SELECT public_ip FROM ops.instances WHERE role = 'primary' AND public_ip IS NOT NULL ORDER BY last_seen_at DESC NULLS LAST LIMIT 1", ())
    if not ok:
        return False, rows
    return (True, rows[0][0]) if rows else (False, "primary_not_registered")


def baseline_p95(config):
    ok, rows = call(config, "SELECT baseline_p95_seconds FROM ops.incidents WHERE fingerprint = %s AND state = 'open'", (INCIDENT,))
    if not ok:
        return False, rows
    return True, (rows[0][0] if rows else None)


def probe(ip, ca_bundle):
    """Direct probe from the management VM: (/health/live ok, /health/ready ok) on ip:8443. Any error is not ok."""
    out = []
    for path in ("/health/live", "/health/ready"):
        try:
            out.append(requests.get(f"https://{ip}:8443{path}", timeout=4, verify=ca_bundle or True).status_code == 200)
        except requests.RequestException:
            out.append(False)
    return tuple(out)
