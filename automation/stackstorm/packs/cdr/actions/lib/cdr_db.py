"""Shared database helpers for the capacity actions (docs/06 action contracts).

Every capacity change goes through the ops.* SECURITY DEFINER functions of migration ops/0009; these
actions never write capacity tables directly. Only short error codes are returned or logged, because
driver messages can contain connection strings.
"""
import logging
import time

import psycopg

LOG = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10
CONNECT_ATTEMPTS = 3  # retried only while connecting: nothing has run yet, so a retry cannot repeat a write
CONNECT_BACKOFF_SECONDS = 1
SESSION_OPTIONS = "-c statement_timeout=15000 -c lock_timeout=5000"

# SQLSTATEs raised by ops/0009.
SQLSTATE_CODES = {
    "CDR01": "lease_lost",
    "CDR02": "slot_conflict",
    "CDR03": "invalid_transition",
    "CDR04": "bad_argument",
}


def classify(exc):
    state = getattr(exc, "sqlstate", None)
    if state in SQLSTATE_CODES:
        return SQLSTATE_CODES[state]
    if isinstance(exc, psycopg.OperationalError):
        return "connect_or_network"
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return "privilege"
    return type(exc).__name__[:60]


def connect(url):
    # No named prepared statements: the Supabase session pooler is used for every role (docs/05).
    # Server-side limits keep a stuck statement or lock wait from outliving the action; TCP keepalives and
    # tcp_user_timeout make a silently dropped pooler connection fail in ~20 s instead of hanging.
    return psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_SECONDS, autocommit=True, prepare_threshold=None,
                           options=SESSION_OPTIONS, keepalives=1, keepalives_idle=10, keepalives_interval=5, keepalives_count=3,
                           tcp_user_timeout=20000)


def _log_failure(conn, code, started):
    """Log elapsed_ms and the pids blocking this session (best effort; codes and numbers only, never driver text)."""
    elapsed_ms = int((time.monotonic() - started) * 1000)
    blocking = "unavailable"
    try:
        if not conn.closed:
            blocking = str(conn.execute("SELECT pg_blocking_pids(pg_backend_pid())").fetchone()[0])
    except Exception:  # noqa: BLE001 - diagnostics must never mask the original failure
        pass
    LOG.warning("cdr_db failure code=%s elapsed_ms=%d blocking_pids=%s", code, elapsed_ms, blocking)


def call(config, sql, params, attempts=CONNECT_ATTEMPTS):
    """Run one function call. Returns (True, rows) or (False, short_code).

    Only the connect step is retried (`attempts` times, linear backoff): a transient pooler or network blip should not
    fail a capacity task. Once connected the statement runs once; its failure is reported, never repeated, because
    several ops functions append events or move state."""
    url = config.get("supabase_ops_url")
    if not url:
        return False, "config_missing"
    conn = None
    for n in range(max(1, attempts)):
        try:
            conn = connect(url)
            break
        except psycopg.OperationalError:
            if n + 1 >= max(1, attempts):
                return False, "connect_or_network"
            time.sleep(CONNECT_BACKOFF_SECONDS * (n + 1))
        except Exception as exc:  # noqa: BLE001 - reported by code only
            return False, classify(exc)
    started = time.monotonic()
    try:
        with conn:
            try:
                return True, conn.execute(sql, params).fetchall()
            except Exception as exc:  # noqa: BLE001 - reported by code only
                code = classify(exc)
                _log_failure(conn, code, started)  # inside `with`: the session is still open to ask pg_blocking_pids
                return False, code
    except Exception as exc:  # noqa: BLE001 - e.g. close failed
        return False, classify(exc)
