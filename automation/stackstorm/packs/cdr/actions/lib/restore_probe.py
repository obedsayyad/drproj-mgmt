"""Restore probe (T18 heavy half, docs/08 and docs/09): pg_dump of the app schema from the source, pg_restore of that dump into a scratch
database on the restore-test PostgreSQL, table and row counts compared, scratch database dropped, dump deleted. Runs only inside
cdr.run_heavy, so it holds the lease and the heavy policy keeps it to one at a time. It is NOT the P7 restore: it never touches the live app
schema, never reads a catalogue entry and writes no backup file that outlives the call.

Credentials reach pg_dump/pg_restore through PG* environment variables of a minimal child environment, never argv. Only short codes and
counts are returned or logged (driver and tool messages can contain connection strings).
"""
import os
import shutil
import subprocess
import tempfile
from urllib.parse import parse_qs, unquote, urlsplit

import psycopg
from psycopg import sql

TIMEOUT_S = 600
KEEP_ENV = ("PATH", "PGSSLROOTCERT", "PGSSLMODE", "LANG")


def _pg_env(url, dbname=None):
    u = urlsplit(url)
    env = {k: os.environ[k] for k in KEEP_ENV if k in os.environ}
    env.update({"PGHOST": u.hostname or "", "PGPORT": str(u.port or 5432), "PGUSER": unquote(u.username or ""),
                "PGPASSWORD": unquote(u.password or ""), "PGDATABASE": dbname or (u.path or "/").lstrip("/") or "postgres"})
    mode = (parse_qs(u.query).get("sslmode") or [None])[0]
    if mode:
        env["PGSSLMODE"] = mode
    return env


def _run(argv, env, timeout):
    try:
        return subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False).returncode
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return 127


def _counts(conn):
    tables = [r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'app' AND table_type = 'BASE TABLE' ORDER BY 1")]
    rows = {t: conn.execute(sql.SQL("SELECT count(*) FROM app.{}").format(sql.Identifier(t))).fetchone()[0] for t in tables}
    return rows


def probe(source_url, target_url, operation_id, pg_dump="pg_dump", pg_restore="pg_restore", timeout=TIMEOUT_S):
    scratch = "probe_" + "".join(c for c in str(operation_id) if c.isalnum())[:16].lower()
    work = tempfile.mkdtemp(prefix="cdr-probe-")
    os.chmod(work, 0o700)
    created = False
    try:
        dump = os.path.join(work, "app.dump")
        rc = _run([pg_dump, "-Fc", "-n", "app", "--no-owner", "--no-privileges", "-f", dump], _pg_env(source_url), timeout)
        if rc is None:
            return {"result": "failed", "code": "dump_timeout"}
        if rc != 0 or not os.path.exists(dump):
            return {"result": "failed", "code": "dump_failed"}
        os.chmod(dump, 0o600)
        size = os.path.getsize(dump)
        try:
            with psycopg.connect(target_url, autocommit=True, connect_timeout=10) as admin:
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(scratch)))
                created = True
        except psycopg.Error:
            return {"result": "failed", "code": "target_unavailable"}
        rc = _run([pg_restore, "--no-owner", "--no-privileges", "--exit-on-error", "-d", scratch, dump], _pg_env(target_url, scratch), timeout)
        if rc is None:
            return {"result": "failed", "code": "restore_timeout", "dump_bytes": size}
        if rc != 0:
            return {"result": "failed", "code": "restore_failed", "dump_bytes": size}
        try:
            with psycopg.connect(source_url, autocommit=True, connect_timeout=10) as src:
                want = _counts(src)
            with psycopg.connect(target_url, dbname=scratch, autocommit=True, connect_timeout=10) as dst:
                got = _counts(dst)
        except psycopg.Error:
            return {"result": "failed", "code": "verify_unavailable", "dump_bytes": size}
        out = {"dump_bytes": size, "tables_source": len(want), "tables_restored": len(got), "rows_source": sum(want.values()), "rows_restored": sum(got.values())}
        if set(want) != set(got) or not got:
            return {"result": "failed", "code": "restore_tables_mismatch", **out}
        return {"result": "ok", **out}
    finally:
        if created:
            try:
                with psycopg.connect(target_url, autocommit=True, connect_timeout=10) as admin:
                    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(scratch)))
            except psycopg.Error:
                pass  # reported by the caller's leftover check; the scratch name carries the operation id
        shutil.rmtree(work, ignore_errors=True)
