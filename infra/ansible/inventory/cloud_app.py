#!/usr/bin/env python3
"""Dynamic Ansible inventory for the cloud_app group, read from ops.instances (docs/07).

Reads with the dr_automation role (SELECT only) over DR_AUTOMATION_URL; the URL is never printed. Only bound, real (not simulated)
DigitalOcean/Linode instances with a public_ip are listed. A database error is a hard failure (exit 1): an unreadable table is never
treated as "no instances".

  cloud_app.py --list
  cloud_app.py --host <instance_id>
"""
import argparse
import json
import os
import sys

QUERY = """
SELECT i.id::text, s.slot_key, i.provider, i.public_ip::text, i.image_digest
FROM ops.capacity_slots s JOIN ops.instances i ON i.id = s.bound_instance_id
WHERE i.provider IN ('digitalocean', 'linode') AND NOT i.simulated AND i.public_ip IS NOT NULL
  AND i.lifecycle_state IN ('deploying', 'ready', 'draining', 'unknown')
ORDER BY s.slot_key
"""
SSH_KEY_ENV = "CLOUD_VM_SSH_KEY_FILE"
# The Terraform-provisioned first-login user (provider image default). Exposed as cloud_first_contact_user, not ansible_user: the bootstrap
# role creates `drproj`, and group_vars/cloud_app.yml makes `drproj` the connection user for every play except the first-contact one.
DEFAULT_USER = "root"


def fetch(url):
    import psycopg

    with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
        return conn.execute(QUERY).fetchall()


def build(rows):
    hostvars = {}
    for instance_id, slot_key, provider, ip, digest in rows:
        hostvars[instance_id] = {
            "ansible_host": str(ip).split("/")[0],
            "cloud_first_contact_user": os.environ.get("CLOUD_VM_SSH_USER", DEFAULT_USER),
            "instance_id": instance_id,
            "slot_key": slot_key,
            "provider": provider,
            "recorded_image_digest": digest,
        }
        if os.environ.get(SSH_KEY_ENV):
            hostvars[instance_id]["ansible_ssh_private_key_file"] = os.environ[SSH_KEY_ENV]
    return {"cloud_app": {"hosts": sorted(hostvars)}, "_meta": {"hostvars": hostvars}}


def main(argv=None):
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true")
    g.add_argument("--host")
    args = ap.parse_args(argv)
    url = os.environ.get("DR_AUTOMATION_URL")
    if not url:
        print("cloud_app: DR_AUTOMATION_URL is not set", file=sys.stderr)
        return 1
    try:
        inv = build(fetch(url))
    except Exception as exc:  # noqa: BLE001 - driver messages can carry the connection string; print the type only
        print(f"cloud_app: database read failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    out = inv if args.list else inv["_meta"]["hostvars"].get(args.host, {})
    json.dump(out, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
