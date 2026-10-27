"""Same harness as test_keepalive.py (KEEPALIVE_TEST_URL, TEST_DATABASE_URL). Needs migration ops/0008."""
import os
import sys

import psycopg
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "actions"))
from record_alert import RecordAlert, parse  # noqa: E402

URL = os.environ.get("KEEPALIVE_TEST_URL")
ADMIN = os.environ.get("TEST_DATABASE_URL")


def alert(status="firing", fingerprint="abc123", **labels):
    return {"status": status, "fingerprint": fingerprint, "labels": {"alertname": "CdrBackupStale", "policy_version": "1", **labels}}


def test_parse_accepts_only_firing_alerts_with_fingerprint_and_policy_version():
    assert parse(alert()) == ("abc123", 1, "CdrBackupStale")
    assert parse(alert(status="resolved")) is None
    assert parse(alert(fingerprint="NOT HEX")) is None
    assert parse(alert(policy_version="0")) is None
    assert parse(alert(policy_version="x")) is None
    assert parse({"status": "firing", "fingerprint": "ab"}) is None


@pytest.mark.skipif(not (URL and ADMIN), reason="needs KEEPALIVE_TEST_URL and TEST_DATABASE_URL")
def test_repeated_firing_coalesces_and_resolved_is_skipped():
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DELETE FROM ops.processed_events WHERE source = 'alertmanager'")
    a = RecordAlert.__new__(RecordAlert)
    a.config = {"supabase_ops_url": URL}
    body = {"alerts": [alert(), alert(status="resolved", fingerprint="def456"), alert(policy_version="")]}
    assert a.run(body) == (True, {"recorded": 1, "skipped": 2})
    assert a.run(body) == (True, {"recorded": 1, "skipped": 2})
    with psycopg.connect(ADMIN, autocommit=True) as c:
        rows = c.execute("SELECT fingerprint, policy_version, source, event_name, last_seen_at > first_seen_at FROM ops.processed_events").fetchall()
    assert rows == [("abc123", 1, "alertmanager", "CdrBackupStale", True)]
