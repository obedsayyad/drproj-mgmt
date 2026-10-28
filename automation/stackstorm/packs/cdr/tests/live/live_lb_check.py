#!/usr/bin/env python3
"""P6.2 live read-only check of the real HAProxy routing adapter. Run from the st2actionrunner container on management (it holds the runner
client certificate and the ops login):  python tests/live/live_lb_check.py --config /path/to/cdr-pack-config.yaml
Not collected by pytest (no test_ prefix); tests/test_live_lb_check.py drives run() against the fake fe_admin so the logic is covered in CI.

Allowed live commands, enforced by Guard before anything is sent (user-approved for P6.2):
  show servers state | show servers conn        read-only
  set server be_app/primary weight <its CURRENT weight>   a no-op that still exercises fence, dump and audit
  set server be_app/iwarm_act state ready|maint           round trip on a line that is disabled
The primary is never put in maint or drain, and nothing else is changed. Exit 0 only when every check passed."""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for sub in ("actions", os.path.join("actions", "lib")):
    sys.path.insert(0, os.path.join(HERE, "..", "..", sub))

import haproxy_admin as hx  # noqa: E402
from haproxy_admin import AdminClient, AdminError  # noqa: E402
from haproxy_routing import Router  # noqa: E402

ROUND_TRIP_LINE = "iwarm_act"


class Guard(AdminClient):
    """Refuses every mutation outside the approved set; the primary weight must equal the weight observed at the start."""

    def __init__(self, inner, primary_weight):
        self.inner, self.primary_weight = inner, primary_weight

    def show_state(self):
        return self.inner.show_state()

    def show_conn(self):
        return self.inner.show_conn()

    def set_cmd(self, cmd):
        allowed = {f"set server be_app/primary weight {self.primary_weight}\n",
                   f"set server be_app/{ROUND_TRIP_LINE} state ready\n", f"set server be_app/{ROUND_TRIP_LINE} state maint\n"}
        if cmd not in allowed:
            raise hx.CommandRefused("live_guard_refused")
        return self.inner.set_cmd(cmd)


def run(client, store, fence, audit, count_audit):
    """-> (ok, report dict). count_audit() returns the number of haproxy.command audit rows of this operation."""
    report, ok = {"checks": {}}, True

    def check(name, cond, detail=None):
        nonlocal ok
        report["checks"][name] = {"ok": bool(cond), **({"detail": detail} if detail is not None else {})}
        ok = ok and bool(cond)

    text, m = client.show_state()
    check("show_servers_state_parses", len(m.servers) >= 15 and "primary" in m.servers, {"servers": len(m.servers)})
    check("primary_is_not_touched_by_this_check", True, {"primary_weight": m.get("primary").weight, "primary_up": m.get("primary").is_up})
    try:
        check("show_servers_conn_parses", "primary" in client.show_conn(), {"servers": sorted(client.show_conn())[:3]})
    except AdminError as exc:
        check("show_servers_conn_parses", False, str(exc))
    guard = Guard(client, m.get("primary").weight)
    router = Router(guard, store, fence, audit)
    start_dumps = len(os.listdir(store.dir)) if os.path.isdir(store.dir) else 0
    initial_line = m.get(ROUND_TRIP_LINE)
    try:
        router.apply([f"set server be_app/primary weight {m.get('primary').weight}\n"])
        check("primary_weight_noop", router.membership().get("primary").weight == m.get("primary").weight)
        try:
            router.apply([f"set server be_app/{ROUND_TRIP_LINE} state ready\n"])
            check("iwarm_act_ready", router.membership().get(ROUND_TRIP_LINE).is_ready)
        except AdminError as exc:
            check("iwarm_act_ready", False, str(exc))
        finally:
            if not router.membership().get(ROUND_TRIP_LINE).is_maint:
                router.apply([f"set server be_app/{ROUND_TRIP_LINE} state maint\n"])
        check("iwarm_act_back_to_maint", router.membership().get(ROUND_TRIP_LINE).is_maint)
    except AdminError as exc:
        check("sequence", False, str(exc))
    after = router.membership()
    check("iwarm_act_final_state_unchanged", (after.get(ROUND_TRIP_LINE).admin_state, after.get(ROUND_TRIP_LINE).addr) == (initial_line.admin_state, initial_line.addr))
    dumps = len(os.listdir(store.dir)) - start_dumps
    check("dumps_written", dumps >= 3, {"new_dumps": dumps})
    check("audit_rows_written", count_audit() >= 3, {"rows": count_audit()})
    report["primary_final"] = {"weight": after.get("primary").weight, "up": after.get("primary").is_up, "maint": after.get("primary").is_maint}
    check("primary_unchanged", report["primary_final"]["weight"] == m.get("primary").weight and after.get("primary").is_maint == m.get("primary").is_maint)
    return ok, report


def main(argv):
    import yaml

    from acquire_lease import AcquireLease
    from cdr_common import advance
    from cdr_db import call
    from lease_keeper import fenced
    from open_operation import OpenOperation
    from release_lease import ReleaseLease
    from providers.real import RealAdapter
    import json as _json

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="the pack config file (cdr-pack-config.yaml)")
    cfg = yaml.safe_load(open(ap.parse_args(argv).config))
    adapter = RealAdapter(cfg["real_adapter"])
    import time

    def act(cls):
        a = cls.__new__(cls)
        a.config = cfg
        return a

    ok, op = act(OpenOperation).run("reconcile", "automation", f"live-test:p6.2:{int(time.time())}")
    if not ok:
        print(_json.dumps({"error": op}))
        return 2
    op_id = op["operation_id"]
    for _ in range(6):  # the reconcile timer holds the lease for a few seconds at a time
        ok, lease = act(AcquireLease).run(op_id)
        if ok and lease.get("acquired"):
            break
        time.sleep(5)
    else:
        print(_json.dumps({"error": "lease_unavailable"}))
        # a queued operation can only end as a terminal no-op (ops._operation_transition_ok), never as failed
        advance(cfg, op_id, "operation.succeeded", "live_check", "succeeded", "live check could not take the lease", {"noop_reason": "lease_unavailable"})
        return 2
    gen = lease["generation"]
    advance(cfg, op_id, "operation.started", "live_check", "running", "P6.2 live read-only check")
    try:
        fence = lambda: fenced(cfg, op_id, gen, lambda: None)  # noqa: E731

        def audit(cmd, old, new, dump):
            ok_, out = call(cfg, "SELECT ops.record_haproxy_command(%s::uuid, %s, %s, %s::jsonb, %s::jsonb, %s)", (op_id, gen, cmd, _json.dumps(old), _json.dumps(new), dump))
            if not ok_:
                raise AdminError("audit_failed")

        def count():
            ok_, rows = call(cfg, "SELECT count(*) FROM ops.audit_events WHERE action = 'haproxy.command' AND operation_id = %s::uuid", (op_id,))
            return rows[0][0] if ok_ else -1

        passed, report = run(adapter.admin, adapter.store, fence, audit, count)
        report["operation_id"] = op_id
        print(_json.dumps(report, indent=2, default=str))
        advance(cfg, op_id, "operation.succeeded" if passed else "operation.failed", "done", "succeeded" if passed else "failed",
                "P6.2 live check", {"live_check": passed} if passed else {"error": "live_check_failed"})
        return 0 if passed else 1
    finally:
        act(ReleaseLease).run(op_id, gen)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
