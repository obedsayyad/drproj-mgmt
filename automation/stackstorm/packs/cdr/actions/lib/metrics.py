"""CPU-signal freshness shared by the capacity gates. Stale or unknown metrics never authorize scaling (docs/04).

Order: the `metrics.stale` flag (set from Alertmanager by cdr.alert_intent) always blocks. Then, with `prometheus_url`
set, every expected job must be up and freshly scraped; without it only the simulation adapter may answer, from the
`metrics_fresh` flag of its state file (reported as metrics_source=simulation); real mode without a source is stale.
"""
import json
import re
import urllib.request
from datetime import datetime, timezone

from cdr_db import call


_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}(?::?\d{2})?)$")


def parse_scrape_time(value):
    """Prometheus reports lastScrape with nanoseconds ("2026-10-29T15:31:20.790539617Z"); fromisoformat takes at most microseconds."""
    m = _TS.match(value)
    if not m:
        raise ValueError(value)
    off = m.group(3)
    if off == "Z":
        off = "+00:00"
    elif len(off) == 3:
        off += ":00"
    elif len(off) == 5:
        off = off[:3] + ":" + off[3:]
    return datetime.fromisoformat(m.group(1) + (m.group(2) or "")[:7] + off)


def prometheus_fresh(url, jobs, stale_after):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/v1/targets?state=active", timeout=2) as r:  # noqa: S310 - operator-configured URL
            targets = json.load(r)["data"]["activeTargets"]
    except Exception:  # noqa: BLE001
        return False
    now = datetime.now(timezone.utc)
    for job in jobs:
        mine = [t for t in targets if t.get("labels", {}).get("job") == job]
        if not mine:
            return False
        for t in mine:
            try:
                scraped = parse_scrape_time(t["lastScrape"]) if t.get("lastScrape") else None
            except ValueError:
                return False  # an unreadable timestamp is not evidence of freshness
            if t.get("health") != "up" or scraped is None or (now - scraped).total_seconds() > stale_after:
                return False
    return True


def metrics_state(config, adapter, stale_after_seconds):
    """-> (ok, {"fresh": bool, "source": str}) or (False, code)."""
    ok, rows = call(config, "SELECT metrics_stale_since IS NOT NULL FROM ops.runtime_flags WHERE singleton", ())
    if not ok:
        return False, rows
    if rows and rows[0][0]:
        return True, {"fresh": False, "source": "alert"}
    url = config.get("prometheus_url")
    if url:
        jobs = [j for j in (config.get("prometheus_expected_jobs") or "relief").split(",") if j]
        return True, {"fresh": prometheus_fresh(url, jobs, stale_after_seconds), "source": "prometheus"}
    if adapter.simulated:
        return True, {"fresh": adapter.metrics_fresh(), "source": "simulation"}
    return True, {"fresh": False, "source": "none"}
