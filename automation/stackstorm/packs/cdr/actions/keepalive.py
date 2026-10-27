"""Supabase Free keep-alive (docs/09): SELECT 1 through the session pooler, then record the outcome.

Only a short error class is stored or returned; connection strings and driver messages are never
logged or written, because they can contain credentials.
"""
import time

import psycopg
from st2common.runners.base_action import Action

CONNECT_TIMEOUT_SECONDS = 10


def classify(exc):
    """Short, credential-free label for a failure."""
    if isinstance(exc, psycopg.OperationalError):
        return "connect_or_network"
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return "privilege"
    return type(exc).__name__[:60]


class KeepAlive(Action):
    def run(self):
        url = self.config.get("supabase_ops_url")
        if not url:
            return False, "config_missing"
        started = time.time()
        error = None
        try:
            with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_SECONDS, autocommit=True) as conn:
                conn.execute("SELECT 1")
        except Exception as exc:  # noqa: BLE001 - recorded by class only
            error = classify(exc)
        try:
            self._record(url, started, error)
        except Exception as exc:  # noqa: BLE001
            return False, "record_failed:" + classify(exc)
        return (False, error) if error else (True, "ok")

    def _record(self, url, started, error):
        with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_SECONDS, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO ops.keepalive_runs (started_at, succeeded, source, detail)"
                " VALUES (to_timestamp(%s), %s, 'stackstorm', %s)",
                (started, error is None, error),
            )
