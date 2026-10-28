"""The live check script's logic (tests/live/live_lb_check.py) against the fake fe_admin: the approved sequence passes and leaves the LB as it
found it; the guard refuses anything else (primary state, other weights, other lines) before it is sent."""
import importlib.util
import os

import pytest

import haproxy_admin as hx
from test_haproxy_admin import MAINT, FakeAdmin, Hooks, client, fake, pki  # noqa: F401

_spec = importlib.util.spec_from_file_location("live_lb_check", os.path.join(os.path.dirname(__file__), "live", "live_lb_check.py"))
live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live)


def servers_with_15():
    s = {n: v for n, v in __import__("test_haproxy_admin").base_servers().items()}
    for i in range(3, 6):
        s[f"elastic-do-{i}"] = ["0.0.0.0", 8443, 0, 0x04, 1]
    for i in range(2, 6):
        s[f"elastic-linode-{i}"] = ["0.0.0.0", 8443, 0, 0x04, 1]
    return s


def test_the_approved_sequence_passes_and_restores_iwarm_act(pki, tmp_path):
    f = FakeAdmin(pki, servers_with_15())
    try:
        f.scur["primary"] = [0]
        f.servers["iwarm_act"][3] = MAINT
        f.overrides["show servers conn"] = "# bkname/svname bkid/svid addr port - purge_delay used_cur used_max need_est unsafe_nb safe_nb idle_nb idle_conn_cur idle_conn_max\nbe_app/primary 3/1 10.0.0.1 8443 - 5000 0 0 0 0 0 0 0 0\n"
        c = client(f, pki)
        hooks, rows = Hooks(), []
        store = hx.MembershipStore(str(tmp_path / "st"))
        ok, report = live.run(c, store, hooks.fence, lambda *a: (hooks.audit(*a), rows.append(a[0])), lambda: len(rows))
        assert ok, report
        assert f.mutations() == ["set server be_app/primary weight 100\n", "set server be_app/iwarm_act state ready\n", "set server be_app/iwarm_act state maint\n"]
        assert f.servers["iwarm_act"][3] == MAINT and f.servers["primary"][3] == 0
    finally:
        f.stop = True
        f.sock.close()


@pytest.mark.parametrize("cmd", ["set server be_app/primary state maint\n", "set server be_app/primary state drain\n", "set server be_app/primary weight 7\n",
                                 "set server be_app/warm_act state ready\n", "set server be_app/iwarm_act weight 5\n", "set server be_app/elastic-do-1 state maint\n"])
def test_the_guard_refuses_every_other_mutation_before_it_is_sent(pki, cmd):
    f = FakeAdmin(pki)
    try:
        g = live.Guard(client(f, pki), primary_weight=100)
        with pytest.raises(hx.CommandRefused):
            g.set_cmd(cmd)
        assert f.received == []
    finally:
        f.stop = True
        f.sock.close()


def test_an_iwarm_act_that_will_not_become_ready_is_reported_and_put_back_in_maint(pki, tmp_path):
    f = FakeAdmin(pki, servers_with_15())
    try:
        f.servers["iwarm_act"][3] = 0x04  # config-disabled; the fake ignores `state ready` for this line (as HAProxy may)
        orig = f._answer

        def answer(cmd):
            if cmd == "set server be_app/iwarm_act state ready\n":
                return ""
            return orig(cmd)

        f._answer = answer
        hooks, rows = Hooks(), []
        ok, report = live.run(client(f, pki), hx.MembershipStore(str(tmp_path / "st")), hooks.fence, lambda *a: rows.append(a[0]), lambda: len(rows))
        assert not ok and report["checks"]["iwarm_act_ready"]["ok"] is False
        assert report["checks"]["primary_unchanged"]["ok"] is True
    finally:
        f.stop = True
        f.sock.close()
