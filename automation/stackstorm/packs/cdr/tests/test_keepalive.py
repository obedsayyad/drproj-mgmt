"""Run with the st2 runner's Python (needs st2common and psycopg):
    pytest tests/ -q     (from the pack directory, TEST_DATABASE_URL pointing at a migrated database)
Integration-style on purpose: the point is that dr_ops can really append the row.
"""
import os
import sys

import psycopg
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "actions"))
from keepalive import KeepAlive  # noqa: E402

URL = os.environ.get("KEEPALIVE_TEST_URL")  # login role that is a member of dr_ops
ADMIN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not (URL and ADMIN), reason="needs KEEPALIVE_TEST_URL and TEST_DATABASE_URL")


def action(url):
    a = KeepAlive.__new__(KeepAlive)
    a.config = {"supabase_ops_url": url}
    return a


def rows():
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DELETE FROM ops.keepalive_runs")
    return lambda: psycopg.connect(ADMIN, autocommit=True).execute(
        "SELECT succeeded, source, detail FROM ops.keepalive_runs ORDER BY finished_at"
    ).fetchall()


def test_success_appends_a_succeeded_row():
    read = rows()
    ok, msg = action(URL).run()
    assert (ok, msg) == (True, "ok")
    assert read() == [(True, "stackstorm", None)]


def test_unreachable_database_fails_without_leaking_the_url():
    ok, msg = action("postgres://user:s3cret@127.0.0.1:1/db").run()
    assert ok is False
    assert "s3cret" not in msg and "127.0.0.1" not in msg


def test_missing_config_fails_closed():
    assert action("").run() == (False, "config_missing")
