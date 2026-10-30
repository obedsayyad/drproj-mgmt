#!/usr/bin/env python3
"""Record what a deploy left running on one instance by calling ops.record_instance_deploy (migration ops/0018) as dr_automation.

  record_deploy.py <instance ref> <sha256:digest> <app version> <configuration revision>

<instance ref> is an ops.instances id (cloud VMs) or the provider_resource_id of the fixed self-hosted host ('primary'). The database URL
comes from DR_AUTOMATION_URL and is never printed. Exit 0 and one JSON line on success; any failure is exit 1 with a short reason.
"""
import json
import os
import re
import sys

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ANSIBLE_VENV_PYTHON = "/opt/ansible/bin/python"  # same fallback as inventory/cloud_app.py: the runner's system python has no driver


def connect(url):
    try:
        import psycopg
    except ImportError:
        py = os.environ.get("CLOUD_APP_PYTHON", ANSIBLE_VENV_PYTHON)
        if os.path.abspath(py) == os.path.abspath(sys.executable) or not os.access(py, os.X_OK):
            raise
        os.execv(py, [py, *sys.argv])
    return psycopg.connect(url, connect_timeout=10, autocommit=True)


def record(conn, ref, digest, version, revision):
    """Resolve the instance, call the ops function, return the instance id. `conn` needs .execute(sql, params).fetchone()."""
    if UUID.match(ref):
        instance_id = ref
    else:
        row = conn.execute("SELECT id::text FROM ops.instances WHERE provider = 'self_hosted' AND provider_resource_id = %s", (ref,)).fetchone()
        if row is None:
            raise LookupError(f"no ops.instances row for fixed host {ref!r}")
        instance_id = row[0]
    conn.execute("SELECT ops.record_instance_deploy(%s::uuid, %s, %s, %s)", (instance_id, digest, version, revision))
    return instance_id


def main(argv, connect_fn=connect, environ=os.environ):
    if len(argv) != 4:
        print("usage: record_deploy.py <instance> <digest> <version> <revision>", file=sys.stderr)
        return 2
    url = environ.get("DR_AUTOMATION_URL")
    if not url:
        print("DR_AUTOMATION_URL is not set", file=sys.stderr)
        return 1
    try:
        with connect_fn(url) as conn:
            instance_id = record(conn, *argv)
    except Exception as exc:  # never echo the URL; the driver's message does not contain the password
        print(f"record_instance_deploy failed: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}", file=sys.stderr)
        return 1
    print(json.dumps({"recorded": True, "instance_id": instance_id, "image_digest": argv[1]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
