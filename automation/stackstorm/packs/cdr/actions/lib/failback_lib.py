"""Shared helpers of the failback actions (docs/04): primary identity, direct health probes, incident baseline."""
import time

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


class ProbeResult(tuple):
    """(live ok, ready ok) plus .detail: one short credential-free entry per failed path (status or exception class, seconds)."""
    detail = ()


def probe(ip, ca_bundle):
    """Direct probe from the management VM: (/health/live ok, /health/ready ok) on ip:8443. Any error is not ok."""
    out, detail = [], []
    for path in ("/health/live", "/health/ready"):
        started = time.monotonic()
        try:
            code = requests.get(f"https://{ip}:8443{path}", timeout=4, verify=ca_bundle or True).status_code
            out.append(code == 200)
            if code != 200:
                detail.append({"path": path, "status": code, "seconds": round(time.monotonic() - started, 2)})
        except requests.RequestException as exc:
            out.append(False)
            detail.append({"path": path, "error": type(exc).__name__, "seconds": round(time.monotonic() - started, 2)})
    result = ProbeResult(out)
    result.detail = tuple(detail)
    return result
