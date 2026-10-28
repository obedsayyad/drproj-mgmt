"""Routing modes on the be_app server lines (docs/04, docs/08), as pure planning plus a fenced mutator.

plan_mode() maps a mode to the commands needed to reach it from the observed membership (no-op commands are omitted; "ready" and
weight changes precede "maint"/"drain" so the backend never loses its last serving line mid-plan). Router.apply() runs a plan:
every command is preceded by assert_lease (fence), followed by a `show servers state` dump stored in the versioned membership
store and by an audit record, and its effect is verified from that dump (an unverified effect is an error, never assumed success).
"""
import re

import haproxy_admin as hx
from haproxy_admin import AdminError

FULL = hx.MAX_WEIGHT
PRIMARY = "primary"
UNBOUND_ADDRS = ("", "0.0.0.0")


def lines_for_slot(slot_key):
    """Slot key -> (backup line, active line); elastic slots have one line named like the slot."""
    if slot_key == "warm":
        return "warm_bk", "warm_act"
    if slot_key.startswith("interim-warm-"):
        return "iwarm_bk", "iwarm_act"
    if re.fullmatch(r"elastic-(do|linode)-[1-5]", slot_key):
        return None, slot_key
    raise AdminError("slot_unknown")


def ramp_primary_weight(step_percent):
    """Primary weight for a target primary share s% when every cloud line is at 256 (docs/03): round(256*s/(100-s)), capped at 256."""
    s = int(step_percent)
    if not 0 <= s <= 100:
        raise AdminError("bad_step")
    if s >= 100:
        return FULL
    return min(FULL, round(FULL * s / (100 - s)))


def plan_mode(mode, m, serving=(), step_percent=None):
    """Commands (strings with newline) that move the observed membership `m` to `mode`.
    mode: normal | overflow | ramp | primary_failure. serving: cloud line names (warm_act, iwarm_act, elastic-*) whose slot is routed
    active or draining in ops (routing_bind.serving_lines). Normal leaves serving elastic lines alone (scale-in drains them); ramp
    puts exactly the serving lines at full weight and ready, so a drained or left line is never revived."""
    want = {}  # name -> ("ready"|"maint", weight|None)
    if mode == "normal":
        want[PRIMARY] = ("ready", FULL)
        want["warm_bk"] = ("ready", None)
        want["warm_act"] = ("maint", None)
        for n in m.servers:
            if n.startswith("elastic-") and n not in serving:
                want[n] = ("maint", None)
    elif mode == "overflow":
        want["warm_act"] = ("ready", FULL)  # warm_bk stays ready: the backup line stays UP so standby alerting never goes quiet
    elif mode == "ramp":
        if step_percent is None:
            raise AdminError("bad_step")
        want[PRIMARY] = ("ready", ramp_primary_weight(step_percent))
        for n in serving:
            want[n] = ("ready", FULL)
    elif mode == "primary_failure":
        want[PRIMARY] = ("maint", None)
    else:
        raise AdminError("bad_mode")
    return _diff(m, want)


def _diff(m, want):
    weights, readies, downs = [], [], []
    for name, (state, weight) in want.items():
        s = m.get(name)
        if state != "maint" and s.addr in UNBOUND_ADDRS:
            continue  # an unbound line stays as configured; it is never made ready
        if weight is not None and s.weight != weight:
            weights.append(f"set server {hx.BACKEND}/{name} weight {weight}\n")
        if state == "ready" and not s.is_ready:
            readies.append(f"set server {hx.BACKEND}/{name} state ready\n")
        elif state == "maint" and not s.is_maint:
            downs.append(f"set server {hx.BACKEND}/{name} state maint\n")
    return weights + readies + downs


def plan_primary(mode, m):
    """Primary only: normal = ready at full weight, maint = out of rotation (failure routing)."""
    if mode == "normal":
        return _diff(m, {PRIMARY: ("ready", FULL)})
    if mode == "maint":
        return _diff(m, {PRIMARY: ("maint", None)})
    raise AdminError("bad_mode")


def plan_slot(slot_key, mode, m, ip=None, port=8443):
    """Per-slot routing (the apply_routing slot modes none|backup|active|draining) -> commands."""
    bk, act = lines_for_slot(slot_key)
    cmds, want = [], {}
    if mode == "active":
        if ip is not None and (m.get(act).addr != ip or m.get(act).port != port):
            cmds.append(f"set server {hx.BACKEND}/{act} addr {ip} port {port}\n")
            m = _with_addr(m, act, ip, port)
        want[act] = ("ready", FULL)
    elif mode == "backup":
        if bk is None:
            raise AdminError("bad_mode")
        want[bk] = ("ready", None)
        want[act] = ("maint", None)
    elif mode == "none":
        want[act] = ("maint", None)
        if bk:
            want[bk] = ("maint", None)
    elif mode == "draining":
        for n in (act, bk):
            if n and not m.get(n).is_drain and not m.get(n).is_maint:
                cmds.append(f"set server {hx.BACKEND}/{n} state drain\n")
    else:
        raise AdminError("bad_mode")
    return cmds + _diff(m, want)


def _with_addr(m, name, ip, port):
    s = m.get(name)
    srv = dict(m.servers)
    srv[name] = hx.Server(s.name, ip, port, s.op_state, s.admin_state, s.weight)
    return hx.Membership(srv)


def expected_effect(cmd, m):
    """True when membership `m` shows the effect of the `set server` command `cmd`."""
    parts = cmd.split()
    name = parts[2].split("/", 1)[1]
    s = m.servers.get(name)
    if s is None:
        return False
    if parts[3] == "state":
        return {"ready": s.is_ready, "drain": s.is_drain, "maint": s.is_maint}[parts[4]]
    if parts[3] == "weight":
        return parts[4].endswith("%") or s.weight == int(parts[4])
    if parts[3] == "addr":
        return s.addr == parts[4] and s.port == int(parts[6])
    return False


class Router:
    """fence(): assert_lease; audit(cmd, before, after, dump_name): ops record. Both are injected by the action."""

    def __init__(self, client, store, fence, audit):
        self.client, self.store, self.fence, self.audit = client, store, fence, audit

    def membership(self):
        return self.client.show_state()[1]

    def apply(self, cmds):
        """Run commands in order; returns the final Membership. Stops at the first error (remaining commands are not sent)."""
        after = None
        for cmd in cmds:
            self.fence()
            _, before = self.client.show_state()
            self.client.set_cmd(cmd)
            text, after = self.client.show_state()
            dump = self.store.save(text)
            self.audit(cmd, _summary(before, cmd), _summary(after, cmd), dump)
            if not expected_effect(cmd, after):
                raise AdminError("effect_unverified")
        return after if after is not None else self.membership()


def _summary(m, cmd):
    s = m.servers.get(cmd.split()[2].split("/", 1)[1])
    return None if s is None else {"addr": s.addr, "port": s.port, "weight": s.weight, "op_state": s.op_state, "admin_state": s.admin_state}
