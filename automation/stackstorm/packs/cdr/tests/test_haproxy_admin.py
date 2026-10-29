"""Real HAProxy routing adapter (P6.2) against a fake fe_admin: a threaded TLS server that requires a client certificate and answers
the Runtime API subset the adapter uses. Certificates are generated with the openssl CLI. The fake only records and applies what
it receives (the real allowlist is the LB's); tests that expect a refusal assert that NOTHING reached the socket."""
import json
import os
import socket
import ssl
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import haproxy_admin as hx
import haproxy_routing as hr
from haproxy_admin import AdminClient, AdminError, CommandRefused, MembershipStore, ParseError
from providers.real import RealAdapter
from real_cfg import REAL_CFG

LB_ALLOW = Path(__file__).parents[5] / "infra" / "ansible" / "roles" / "lb" / "files" / "haproxy" / "admin_allow.regex"

HEADER = "1\n# be_id be_name srv_id srv_name srv_addr srv_op_state srv_admin_state srv_uweight srv_iweight srv_time_since_last_change srv_check_status srv_check_result srv_check_health srv_check_state srv_agent_state bk_f_forced_id srv_f_forced_id srv_fqdn srv_port srvrecord srv_use_ssl srv_check_port srv_check_addr srv_agent_addr srv_agent_port\n"
CONN_HEADER = "# be_id be_name srv_id srv_name addr port - purge_delay used_cur used_max need_est unsafe_nb safe_nb idle_nb idle_conn_cur idle_conn_max srv_cur_sess\n"
MAINT, DRAIN = 0x01, 0x08


def base_servers():
    """name -> [addr, port, op_state, admin_state, weight]"""
    return {
        "primary": ["10.0.0.1", 8443, 2, 0, 100],
        "warm_bk": ["10.0.0.2", 8443, 2, 0, 100],
        "warm_act": ["10.0.0.2", 8443, 2, MAINT, 100],
        "iwarm_bk": ["0.0.0.0", 8443, 0, 0x04, 1],
        "iwarm_act": ["0.0.0.0", 8443, 0, 0x04, 1],
        "elastic-do-1": ["10.0.0.5", 8443, 2, 0, 256],
        "elastic-do-2": ["10.0.0.6", 8443, 0, MAINT, 256],
        "elastic-linode-1": ["0.0.0.0", 8443, 0, 0x04, 1],
    }


def render_state(servers):
    rows = []
    for i, (n, (addr, port, op, adm, w)) in enumerate(servers.items(), 1):
        rows.append(f"3 be_app {i} {n} {addr} {op} {adm} {w} {w} 100 0 0 0 0 0 0 0 - {port} - 1 0 - - 0")
    return HEADER + "\n".join(rows) + "\n"


def render_conn(servers, scur):
    rows = [f"3 be_app {i} {n} {s[0]} {s[1]} - 5000 0 0 0 0 0 0 0 0 {scur.get(n, 0)}" for i, (n, s) in enumerate(servers.items(), 1)]
    return CONN_HEADER + "\n".join(rows) + "\n"


def sh(*args, input=None):
    subprocess.run(args, check=True, capture_output=True, input=input)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{d}/ca.key", "-out", f"{d}/ca.crt", "-subj", "/CN=test-ca", "-days", "2")
    (d / "san.ext").write_text("subjectAltName=IP:127.0.0.1\n")
    for name, cn, ext in (("server", "127.0.0.1", ["-extfile", f"{d}/san.ext"]), ("client", "drproj-runner", []), ("rogue", "rogue", [])):
        sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{d}/{name}.key", "-out", f"{d}/{name}.csr", "-subj", f"/CN={cn}")
        if name == "rogue":  # signed by a different CA: must be refused by the server
            sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{d}/rca.key", "-out", f"{d}/rca.crt", "-subj", "/CN=rogue-ca", "-days", "2")
            sh("openssl", "x509", "-req", "-in", f"{d}/rogue.csr", "-CA", f"{d}/rca.crt", "-CAkey", f"{d}/rca.key", "-CAcreateserial", "-out", f"{d}/rogue.crt", "-days", "2")
        else:
            sh("openssl", "x509", "-req", "-in", f"{d}/{name}.csr", "-CA", f"{d}/ca.crt", "-CAkey", f"{d}/ca.key", "-CAcreateserial", "-out", f"{d}/{name}.crt", "-days", "2", *ext)
    return d


class FakeAdmin:
    def __init__(self, pki, servers=None):
        self.servers = servers or base_servers()
        self.received, self.overrides, self.scur, self.delay, self.apply = [], {}, {}, 0, True
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(f"{pki}/server.crt", f"{pki}/server.key")
        ctx.load_verify_locations(f"{pki}/ca.crt")
        ctx.verify_mode = ssl.CERT_REQUIRED
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.addr = f"127.0.0.1:{self.sock.getsockname()[1]}"
        self.ctx = ctx
        self.stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(raw,), daemon=True).start()

    def _serve(self, raw):
        try:
            conn = self.ctx.wrap_socket(raw, server_side=True)
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            cmd = data.decode()
            self.received.append(cmd)
            if self.delay:
                time.sleep(self.delay)
            conn.sendall(self._answer(cmd).encode())
            conn.close()
        except (ssl.SSLError, OSError):
            pass

    def _answer(self, cmd):
        for prefix, text in self.overrides.items():
            if cmd.startswith(prefix):
                return text
        if cmd == "show servers state\n":
            return render_state(self.servers)
        if cmd == "show servers conn\n":
            return render_conn(self.servers, {n: (v.pop(0) if len(v) > 1 else v[0]) for n, v in self.scur.items()})
        p = cmd.split()
        if p[:2] == ["set", "server"] and self.apply:
            s = self.servers[p[2].split("/")[1]]
            if p[3] == "state":
                s[3] = {"ready": 0, "drain": DRAIN, "maint": MAINT}[p[4]]
            elif p[3] == "weight":
                s[4] = int(p[4])
            elif p[3] == "addr":
                s[0], s[1] = p[4], int(p[6])
        return ""

    def mutations(self):
        return [c for c in self.received if c.startswith("set server")]


@pytest.fixture()
def fake(pki):
    f = FakeAdmin(pki)
    yield f
    f.stop = True
    f.sock.close()


def client(fake, pki, timeout=2, cert="client"):
    return AdminClient(fake.addr, f"{pki}/{cert}.crt", f"{pki}/{cert}.key", f"{pki}/ca.crt", timeout=timeout)


def membership(servers=None):
    return hx.parse_state(render_state(servers or base_servers()))


# ---------------------------------------------------------------- allowlist
def test_client_allowlist_is_byte_for_byte_the_lb_admin_allow_regex():
    assert list(hx.ALLOW_PATTERNS) == LB_ALLOW.read_text().splitlines()


@pytest.mark.parametrize("cmd", [
    "set server be_app/primary addr 10.0.0.9 port 8443\n",          # addr on primary
    "set server be_app/warm_bk weight 999\n",                       # above the 256 cap (LB would accept 3 digits)
    "set server be_app/warm_bk addr 10.0.0.999 port 8443\n",        # not an IPv4 address
    "set server be_app/warm_bk addr 10.0.0.1 port 0\n",
    "show info\nshutdown sessions server be_app/primary\n",         # smuggled second command
    "show servers state",                                           # no newline
    "disable server be_app/primary\n", "add server be_app/x 1.2.3.4:80\n", "shutdown sessions server be_app/primary\n", "set maxconn global 1\n",
    "set server be_app/primary state stopping\n", "show servers state\n\n", "", None,
])
def test_a_disallowed_command_is_refused_before_anything_is_sent(fake, pki, cmd):
    with pytest.raises(CommandRefused):
        client(fake, pki).send(cmd)
    assert fake.received == []


def test_set_cmd_refuses_a_read_command_and_every_allowed_command_passes_check():
    with pytest.raises(CommandRefused):
        hx.AdminClient("x:1", "c", "k", "ca").set_cmd("show info\n")
    for ok in ("show servers state\n", "show servers conn be_app\n", "set server be_app/primary weight 256\n", "set server be_app/warm_act weight 50%\n",
               "set server be_app/elastic-do-1 state maint\n", "set server be_app/elastic-linode-5 addr 10.1.2.3 port 8443\n"):
        assert hx.check(ok) == ok


# ---------------------------------------------------------------- transport
def test_show_state_round_trip_over_mtls_one_command_per_connection(fake, pki):
    text, m = client(fake, pki).show_state()
    assert text == render_state(base_servers()) and m.get("primary").weight == 100 and m.get("warm_act").is_maint
    assert fake.received == ["show servers state\n"]


def test_a_client_certificate_from_another_ca_gets_no_answer(fake, pki):
    with pytest.raises(AdminError, match="admin_unreachable"):
        client(fake, pki, cert="rogue").show_state()


def test_no_answer_within_the_timeout_is_an_error_not_success(fake, pki):
    fake.delay = 1.5
    start = time.monotonic()
    with pytest.raises(AdminError, match="admin_unreachable"):
        client(fake, pki, timeout=0.5).set_cmd("set server be_app/warm_bk weight 10\n")
    assert time.monotonic() - start < 1.4


def test_unreachable_endpoint_is_an_error(pki):
    c = AdminClient("127.0.0.1:1", f"{pki}/client.crt", f"{pki}/client.key", f"{pki}/ca.crt", timeout=1)
    with pytest.raises(AdminError, match="admin_unreachable"):
        c.show_state()


def test_an_unexpected_answer_to_a_set_command_is_never_assumed_success(fake, pki):
    fake.overrides["set server"] = "No such server.\n"
    with pytest.raises(AdminError, match="admin_answer_unexpected"):
        client(fake, pki).set_cmd("set server be_app/warm_bk weight 10\n")


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize("text,code", [
    ("", "state_header_invalid"),
    ("2\n# be_id be_name\n", "state_header_invalid"),
    (HEADER, "state_no_servers"),
    (HEADER + "3 be_app 1 primary 10.0.0.1 2\n", "state_row_short"),
    (HEADER + "3 be_app 1 primary 10.0.0.1 x 0 100 100 1 0 0 0 0 0 0 0 - 8443 - 1 0 - - 0\n", "state_row_invalid"),
    ("1\n# be_id be_name srv_name\n3 be_app primary\n", "state_columns_missing"),
    (HEADER + "3 be_other 1 x 10.0.0.1 2 0 100 100 1 0 0 0 0 0 0 0 - 8443 - 1 0 - - 0\n", "state_no_servers"),
])
def test_parse_state_rejects_malformed_answers(text, code):
    with pytest.raises(ParseError, match=code):
        hx.parse_state(text)


def test_parse_conn_reads_the_haproxy_3_layout():
    text = ("# bkname/svname bkid/svid addr port - purge_delay used_cur used_max need_est unsafe_nb safe_nb idle_nb idle_conn_cur idle_conn_max\n"
            "be_app/primary 3/1 10.0.0.1 8443 - 5000 4 9 0 0 0 0 0 0\nbe_app/warm_act 3/3 10.0.0.2 8443 - 5000 0 1 0 0 0 0 0 0\nbe_cli/cli 4/1 - - - 5000 7 7 0 0 0 0 0 0\n")
    assert hx.parse_conn(text) == {"primary": 4, "warm_act": 0}
    with pytest.raises(ParseError, match="conn_row_invalid"):
        hx.parse_conn(text.replace(" 4 9 ", " x 9 "))


def test_parse_conn_reads_scur_and_rejects_garbage():
    assert hx.parse_conn(render_conn(base_servers(), {"warm_act": 3}))["warm_act"] == 3
    with pytest.raises(ParseError):
        hx.parse_conn("nothing useful\n")
    with pytest.raises(ParseError):
        hx.parse_conn(CONN_HEADER + "3 be_app 1 primary 10.0.0.1\n")


# ---------------------------------------------------------------- planning (pure)
def test_normal_mode_primary_full_warm_bk_ready_warm_act_maint_unserved_elastic_maint():
    m = membership()
    assert hr.plan_mode("normal", m, serving=("elastic-do-1",)) == ["set server be_app/primary weight 256\n"]
    assert hr.plan_mode("normal", m) == ["set server be_app/primary weight 256\n", "set server be_app/elastic-do-1 state maint\n"]


def test_overflow_brings_warm_act_ready_at_256_and_leaves_warm_bk_ready():
    assert hr.plan_mode("overflow", membership()) == [
        "set server be_app/warm_act weight 256\n", "set server be_app/warm_act state ready\n"]


def test_ramp_weights_follow_round_256_s_over_100_minus_s_and_serving_lines_are_full():
    assert [hr.ramp_primary_weight(s) for s in (0, 10, 50, 90, 99, 100)] == [0, 28, 256, 256, 256, 256]
    cmds = hr.plan_mode("ramp", membership(), serving=("warm_act", "elastic-do-1"), step_percent=10)
    assert cmds == ["set server be_app/primary weight 28\n", "set server be_app/warm_act weight 256\n", "set server be_app/warm_act state ready\n"]
    assert hr.plan_mode("ramp", membership(), serving=("warm_act",), step_percent=0)[0] == "set server be_app/primary weight 0\n"


def test_ramp_never_revives_a_line_that_is_not_serving_and_never_readies_an_unbound_line():
    cmds = hr.plan_mode("ramp", membership(), serving=("elastic-linode-1", "iwarm_act"), step_percent=50)
    assert cmds == ["set server be_app/primary weight 256\n"]  # unbound lines stay as configured; elastic-do-2 (left, maint) untouched


def test_primary_failure_routing_is_primary_maint_only():
    assert hr.plan_mode("primary_failure", membership()) == ["set server be_app/primary state maint\n"]
    assert hr.plan_mode("primary_failure", membership({**base_servers(), "primary": ["10.0.0.1", 8443, 2, MAINT, 100]})) == []


def test_elastic_join_sets_addr_then_ready_and_leave_drains_then_maint():
    m = membership()
    assert hr.plan_slot("elastic-linode-1", "active", m, ip="10.0.0.9") == [
        "set server be_app/elastic-linode-1 addr 10.0.0.9 port 8443\n", "set server be_app/elastic-linode-1 weight 256\n", "set server be_app/elastic-linode-1 state ready\n"]
    assert hr.plan_slot("elastic-do-1", "draining", m) == ["set server be_app/elastic-do-1 state drain\n"]
    assert hr.plan_slot("elastic-do-1", "none", m) == ["set server be_app/elastic-do-1 state maint\n"]
    assert hr.plan_slot("warm", "backup", m) == []  # already warm_bk ready, warm_act maint
    assert hr.plan_slot("warm", "active", m) == ["set server be_app/warm_act weight 256\n", "set server be_app/warm_act state ready\n"]
    with pytest.raises(AdminError):
        hr.plan_slot("primary", "active", m)


def test_backup_mode_points_the_pre_declared_line_at_the_instance_before_enabling_it():
    declared = membership({**base_servers(), "warm_bk": ["0.0.0.0", 8443, 0, MAINT, 100]})
    assert hr.plan_slot("warm", "backup", declared, ip="10.0.0.7") == [
        "set server be_app/warm_bk addr 10.0.0.7 port 8443\n", "set server be_app/warm_bk state ready\n"]
    assert hr.plan_slot("warm", "backup", declared) == ["set server be_app/warm_bk state ready\n"]  # no address known: state only, as before
    already = membership({**base_servers(), "warm_bk": ["10.0.0.7", 8443, 2, 0, 100]})
    assert hr.plan_slot("warm", "backup", already, ip="10.0.0.7") == []


# ---------------------------------------------------------------- Router: fence, dumps, audit, verification
class Hooks:
    def __init__(self):
        self.events = []

    def fence(self, cmd):
        self.events.append("fence")
        return f"intent-{len(self.events)}"

    def audit(self, intent, cmd, old, new, dump):
        self.events.append(("audit", cmd, dump))


def adapter_for(fake, pki, tmp_path, hooks):
    cfg = {**REAL_CFG, "haproxy": {"admin_addr": fake.addr, "client_cert": f"{pki}/client.crt", "client_key": f"{pki}/client.key",
                                  "ca_bundle": f"{pki}/ca.crt", "state_dir": str(tmp_path / "store")}}
    return RealAdapter(cfg).bind(hooks.fence, hooks.audit)


def test_every_mutation_is_fenced_first_then_dumped_and_audited(fake, pki, tmp_path):
    hooks = Hooks()
    a = adapter_for(fake, pki, tmp_path, hooks)
    assert a.set_routing("lb", "overflow") == 2
    assert fake.servers["warm_act"][3:] == [0, 256] and fake.servers["warm_bk"][3] == 0
    kinds = [e if e == "fence" else "audit" for e in hooks.events]
    assert kinds == ["fence", "audit"] * 2
    dumps = sorted(os.listdir(tmp_path / "store"))
    assert len(dumps) == 2 and [e[2] for e in hooks.events if e != "fence"] == dumps
    assert stat.S_IMODE(os.stat(tmp_path / "store").st_mode) == 0o700
    assert all(stat.S_IMODE(os.stat(tmp_path / "store" / d).st_mode) == 0o600 for d in dumps)
    assert hx.parse_state((tmp_path / "store" / dumps[-1]).read_text()).get("warm_bk").is_ready  # the last dump shows the final state
    assert a.set_routing("lb", "overflow") == 0  # idempotent: nothing sent, nothing dumped
    assert len(os.listdir(tmp_path / "store")) == 2


def test_a_lost_lease_stops_before_the_command_is_sent(fake, pki, tmp_path):
    from lease_keeper import LeaseLost

    hooks = Hooks()
    a = adapter_for(fake, pki, tmp_path, hooks)

    def lost(cmd):
        raise LeaseLost("lease_lost")

    a.bind(lost, hooks.audit)
    with pytest.raises(LeaseLost):
        a.set_routing("lb", "primary_failure")
    assert fake.mutations() == []


def test_an_effect_that_does_not_show_in_the_dump_is_an_error(fake, pki, tmp_path):
    fake.apply = False  # answers "" but changes nothing
    with pytest.raises(AdminError, match="effect_unverified"):
        adapter_for(fake, pki, tmp_path, Hooks()).set_routing("lb", "primary_failure")


def test_unbound_adapter_and_missing_haproxy_config_refuse_to_route(fake, pki, tmp_path):
    from providers import AdapterConfigError

    with pytest.raises(AdapterConfigError, match="real_adapter_config_missing"):
        RealAdapter(REAL_CFG).set_routing("lb", "normal")
    cfg = {**REAL_CFG, "haproxy": {"admin_addr": fake.addr, "client_cert": f"{pki}/client.crt", "client_key": f"{pki}/client.key",
                                  "ca_bundle": f"{pki}/ca.crt", "state_dir": str(tmp_path / "s")}}
    with pytest.raises(AdapterConfigError, match="routing_not_bound"):
        RealAdapter(cfg).set_routing("lb", "normal")


def test_elastic_join_then_leave_end_to_end(fake, pki, tmp_path):
    a = adapter_for(fake, pki, tmp_path, Hooks())
    a.set_routing("elastic-linode-1", "active", ip="10.0.0.9")
    s = fake.servers["elastic-linode-1"]
    assert s[0] == "10.0.0.9" and s[1] == 8443 and s[3] == 0 and s[4] == 256
    a.set_routing("elastic-linode-1", "draining")
    assert fake.servers["elastic-linode-1"][3] == DRAIN
    a.set_routing("elastic-linode-1", "none")
    assert fake.servers["elastic-linode-1"][3] == MAINT


# ---------------------------------------------------------------- drain
def test_drain_puts_the_line_in_drain_and_reports_busy_until_scur_is_zero(fake, pki, tmp_path):
    a = adapter_for(fake, pki, tmp_path, Hooks())
    fake.scur["elastic-do-1"] = [4, 2, 0]
    assert [a.drain("digitalocean", "r", "elastic-do-1") for _ in range(3)] == ["busy", "busy", "drained"]
    assert fake.servers["elastic-do-1"][3] == DRAIN and len(fake.mutations()) == 1  # drain set once, then only read


def test_drain_without_a_slot_or_with_unreadable_conn_is_unknown_never_drained(fake, pki, tmp_path):
    a = adapter_for(fake, pki, tmp_path, Hooks())
    assert a.drain("digitalocean", "r") == "unknown"
    fake.overrides["show servers conn"] = "garbage\n"
    assert a.drain("digitalocean", "r", "elastic-do-1") == "unknown"


def test_drain_instance_times_out_without_deleting_and_never_calls_it_drained(fake, pki, tmp_path, monkeypatch):
    import drain_instance as di

    a = adapter_for(fake, pki, tmp_path, Hooks())
    fake.scur["elastic-do-1"] = [7]
    monkeypatch.setattr(di.providers, "load", lambda cfg: a)
    monkeypatch.setattr(di, "bind", lambda cfg, ad, op, gen: ad)
    monkeypatch.setattr(di, "policy_section", lambda cfg, op, sec: (True, {"drain_timeout_seconds": 1} if sec == "scale_in" else {"lease_renewal_seconds": 20}))

    class Keeper:
        def __init__(self, *a):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *e):
            return False
        def check(self):
            pass

    monkeypatch.setattr(di, "LeaseKeeper", Keeper)
    act = di.DrainInstance.__new__(di.DrainInstance)
    act.config = {}
    ok, out = act.run("op", 1, "digitalocean", "r", slot_key="elastic-do-1", poll_seconds=0.2)
    assert ok and out["result"] == "timeout" and out["last_state"] == "busy" and out["simulated"] is False
    assert fake.servers["elastic-do-1"][3] == DRAIN  # left draining; the workflow pauses scale-in and raises infrastructure.drain.timeout


# ---------------------------------------------------------------- membership store
def test_latest_valid_dump_requires_every_expected_line(tmp_path):
    store = MembershipStore(str(tmp_path / "st"))
    full = store.save(render_state(base_servers()))
    partial = dict(base_servers())
    del partial["elastic-do-2"]
    time.sleep(0.002)
    store.save(render_state(partial))
    time.sleep(0.002)
    store.save("not a dump")
    name, text = store.latest_valid(list(base_servers()))
    assert name == full  # the newer partial dump and the garbage dump are skipped
    assert store.latest_valid(["a_line_that_does_not_exist"]) is None
    assert MembershipStore(str(tmp_path / "none")).latest_valid(["primary"]) is None


# ---------------------------------------------------------------- failback gates
FB = {"gate_max_5xx_ratio": 0.01, "gate_max_p95_ratio_to_baseline": 1.5, "gate_max_primary_cpu_percent": 50}


def test_gates_pass_only_with_fresh_samples_and_every_threshold_met():
    from promql import judge

    good = {"5xx": 0.0, "p95": 0.2, "cpu": 20.0, "fresh": 2000.0}
    assert all(g["ok"] for g in judge(good, 0.2, FB, 1000.0).values())
    bad = {**good, "5xx": 0.02}
    assert [k for k, g in judge(bad, 0.2, FB, 1000.0).items() if not g["ok"]] == ["5xx"]
    assert [k for k, g in judge({**good, "p95": 0.31}, 0.2, FB, 1000.0).items() if not g["ok"]] == ["p95"]
    assert [k for k, g in judge({**good, "cpu": 50.0}, 0.2, FB, 1000.0).items() if not g["ok"]] == ["cpu"]
    assert [k for k, g in judge({**good, "fresh": 900.0}, 0.2, FB, 1000.0).items() if not g["ok"]] == ["samples_after_start"]


def test_missing_data_or_missing_baseline_fails_closed():
    from promql import judge

    assert not any(g["ok"] for g in judge({"5xx": None, "p95": None, "cpu": None, "fresh": None}, None, FB, 0).values())
    assert not judge({"5xx": 0, "p95": 0.1, "cpu": 1, "fresh": 5}, None, FB, 0)["p95"]["ok"]


class FakeProm(BaseHTTPRequestHandler):
    values = {}

    def do_GET(self):
        q = parse_qs(urlsplit(self.path).query)["query"][0]
        key = "5xx" if 'code="5xx"' in q and "/" in q else "p95" if "histogram_quantile" in q and "quantile_over_time" not in q else \
            "cpu" if "node_cpu" in q else "fresh" if q.startswith("max(timestamp") else "baseline"
        v = self.values.get(key)
        body = {"status": "success", "data": {"result": [] if v is None else [{"metric": {}, "value": [0, str(v)]}]}}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *a):
        pass


@pytest.fixture()
def prom():
    srv = HTTPServer(("127.0.0.1", 0), FakeProm)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeProm.values = {}
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def observe_action(monkeypatch, prom_url, healthy=True, baseline=0.2, steps=(10, 50, 100)):
    import failback_observe as fo

    class RealLike:
        simulated = False
        mode = "real"
        ca_bundle = None

    monkeypatch.setattr(fo.providers, "load", lambda cfg: RealLike())
    fb = {"primary_traffic_steps_percent": list(steps), "step_observation_seconds": 0.3, "final_step_observation_seconds": 0.5,
          "gate_consecutive_health_successes": 2, **FB}
    monkeypatch.setattr(fo, "policy_section", lambda cfg, op, sec: (True, fb if sec == "failback" else {"lease_renewal_seconds": 20}))
    monkeypatch.setattr(fo, "primary_ip", lambda cfg: (True, "10.0.0.1"))
    monkeypatch.setattr(fo, "baseline_p95", lambda cfg: (True, baseline))
    monkeypatch.setattr(fo, "probe", lambda ip, ca: (healthy, healthy))

    class Keeper:
        def __init__(self, *a):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *e):
            return False
        def check(self):
            pass

    monkeypatch.setattr(fo, "LeaseKeeper", Keeper)
    a = fo.FailbackObserve.__new__(fo.FailbackObserve)
    a.config = {"prometheus_url": prom_url}
    return a


def test_observe_passes_when_probes_and_all_promql_gates_hold(prom, monkeypatch):
    FakeProm.values = {"5xx": 0.001, "p95": 0.25, "cpu": 12.0, "fresh": time.time() + 60}
    ok, out = observe_action(monkeypatch, prom).run("op", 1, 10, poll_seconds=0.1)
    assert ok and out["passed"] is True and out["failed_gates"] == []


def test_observe_regresses_on_high_5xx_p95_cpu_or_stale_samples(prom, monkeypatch):
    good = {"5xx": 0.001, "p95": 0.25, "cpu": 12.0, "fresh": time.time() + 60}
    for key, value, gate in (("5xx", 0.05, "5xx"), ("p95", 0.5, "p95"), ("cpu", 80.0, "cpu"), ("fresh", time.time() - 600, "samples_after_start"), ("cpu", None, "cpu")):
        FakeProm.values = {**good, key: value}
        ok, out = observe_action(monkeypatch, prom).run("op", 1, 50, poll_seconds=0.1)
        assert ok and out["passed"] is False and out["failed_gates"] == [gate], (key, out)


def test_observe_fails_closed_without_a_baseline_and_on_a_failed_direct_probe(prom, monkeypatch):
    FakeProm.values = {"5xx": 0.0, "p95": 0.1, "cpu": 1.0, "fresh": time.time() + 60}
    ok, out = observe_action(monkeypatch, prom, baseline=None).run("op", 1, 10, poll_seconds=0.1)
    assert ok and not out["passed"] and "p95" in out["failed_gates"] and "baseline" in out["failed_gates"]
    ok, out = observe_action(monkeypatch, prom, healthy=False).run("op", 1, 10, poll_seconds=0.1)
    assert ok and out["passed"] is False and out["failed_gates"] == ["health"]


def test_observe_reports_prometheus_unreachable_as_a_failed_gate_and_simulation_as_not_implemented(monkeypatch):
    ok, out = observe_action(monkeypatch, "http://127.0.0.1:1").run("op", 1, 10, poll_seconds=0.1)
    assert ok and out["passed"] is False and out["failed_gates"] == ["prometheus_unreachable"]
    import failback_observe as fo

    class Sim:
        simulated = True

    monkeypatch.setattr(fo.providers, "load", lambda cfg: Sim())
    a = fo.FailbackObserve.__new__(fo.FailbackObserve)
    a.config = {}
    assert a.run("op", 1, 10) == (False, "not_implemented")


# ---------------------------------------------------------------- against a real migrated Postgres (ops/0015)
from test_provisioning_actions import sim  # noqa: E402,F401
from test_capacity_actions import act, db, login_url, op_with_lease  # noqa: E402,F401  (db: skips without TEST_DATABASE_URL)


def routing_action(monkeypatch, adapter, db=None):
    import apply_routing as ar

    monkeypatch.setattr(ar.providers, "load", lambda cfg: adapter)
    a = ar.ApplyRouting.__new__(ar.ApplyRouting)
    a.config = {"supabase_ops_url": login_url()}
    return a


def test_apply_routing_real_writes_one_audit_row_per_command_with_the_dump_name(db, fake, pki, tmp_path, monkeypatch):
    db.execute("DELETE FROM ops.audit_events WHERE action = 'haproxy.command'")
    op, gen = op_with_lease(db)
    a = routing_action(monkeypatch, RealAdapter({**REAL_CFG, "haproxy": {"admin_addr": fake.addr, "client_cert": f"{pki}/client.crt", "client_key": f"{pki}/client.key",
                                                                         "ca_bundle": f"{pki}/ca.crt", "state_dir": str(tmp_path / "st")}}))
    ok, out = a.run(op, gen, "lb", "overflow")
    assert ok and out == {"applied": True, "target": "lb", "mode": "overflow", "simulated": False, "adapter_mode": "real"}
    rows = db.execute("SELECT object, old_value, new_value, operation_id::text FROM ops.audit_events WHERE action = 'haproxy.command' ORDER BY occurred_at, id").fetchall()
    assert [r[0] for r in rows] == ["lb:be_app/warm_act", "lb:be_app/warm_act"] and {r[3] for r in rows} == {op}
    assert [r[2]["command"] + "\n" for r in rows] == fake.mutations()
    assert sorted(r[2]["dump"] for r in rows) == sorted(os.listdir(tmp_path / "st")) and all(r[2]["adapter_mode"] == "real" for r in rows)
    assert rows[0][1]["state"]["weight"] == 100 and rows[0][2]["state"]["weight"] == 256  # old and new server state


def test_apply_routing_real_with_a_fenced_worker_sends_nothing(db, fake, pki, tmp_path, monkeypatch):
    db.execute("DELETE FROM ops.audit_events WHERE action = 'haproxy.command'")
    op, gen = op_with_lease(db)
    a = routing_action(monkeypatch, RealAdapter({**REAL_CFG, "haproxy": {"admin_addr": fake.addr, "client_cert": f"{pki}/client.crt", "client_key": f"{pki}/client.key",
                                                                         "ca_bundle": f"{pki}/ca.crt", "state_dir": str(tmp_path / "st")}}))
    assert a.run(op, gen + 1, "lb", "primary_failure") == (False, "lease_lost")
    assert fake.mutations() == [] and db.execute("SELECT count(*) FROM ops.audit_events WHERE action = 'haproxy.command'").fetchone()[0] == 0


def test_apply_routing_real_rejects_bad_arguments_without_touching_the_lb(db, fake, pki, tmp_path, monkeypatch):
    db.execute("DELETE FROM ops.audit_events WHERE action = 'haproxy.command'")
    op, gen = op_with_lease(db)
    a = routing_action(monkeypatch, RealAdapter({**REAL_CFG, "haproxy": {"admin_addr": fake.addr, "client_cert": f"{pki}/client.crt", "client_key": f"{pki}/client.key",
                                                                         "ca_bundle": f"{pki}/ca.crt", "state_dir": str(tmp_path / "st")}}))
    assert a.run(op, gen, "lb", "ramp") == (False, "bad_argument")            # ramp needs step_percent
    assert a.run(op, gen, "lb", "sideways") == (False, "bad_argument")
    assert a.run(op, gen, "primary", "drain") == (False, "bad_argument")
    assert fake.received == []


def test_an_admin_failure_is_a_short_code_and_the_operation_records_nothing_as_applied(db, fake, pki, tmp_path, monkeypatch):
    op, gen = op_with_lease(db)
    fake.overrides["set server"] = "Unknown command\n"
    a = routing_action(monkeypatch, RealAdapter({**REAL_CFG, "haproxy": {"admin_addr": fake.addr, "client_cert": f"{pki}/client.crt", "client_key": f"{pki}/client.key",
                                                                         "ca_bundle": f"{pki}/ca.crt", "state_dir": str(tmp_path / "st")}}))
    assert a.run(op, gen, "lb", "primary_failure") == (False, "admin_answer_unexpected")
    assert db.execute("SELECT count(*) FROM ops.audit_events WHERE action = 'haproxy.command'").fetchone()[0] == 0


def test_the_incident_baseline_is_set_on_the_open_incident_only(db):
    from failback_lib import INCIDENT

    db.execute("SELECT ops.open_incident(%s, 'critical', 'automation', 'primary failed', ARRAY['primary'])", (INCIDENT,))
    assert db.execute("SELECT ops.set_incident_baseline(%s, 0.18)", (INCIDENT,)).fetchone()[0] is True
    assert db.execute("SELECT baseline_p95_seconds FROM ops.incidents WHERE fingerprint = %s", (INCIDENT,)).fetchone()[0] == pytest.approx(0.18)
    assert db.execute("SELECT ops.set_incident_baseline('nothing.open', 0.18)").fetchone()[0] is False
    with pytest.raises(Exception, match="bad_argument"):
        db.execute("SELECT ops.set_incident_baseline(%s, 0)", (INCIDENT,))
    import failback_lib

    assert failback_lib.baseline_p95({"supabase_ops_url": login_url()}) == (True, pytest.approx(0.18))


def test_the_audit_function_refuses_a_non_server_command_and_a_forged_dump_name(db):
    op, gen = op_with_lease(db)
    for cmd, dump in (("show info\n", "20260101T000000000000Z.state"), ("set server be_app/primary weight 256\n", "../../etc/passwd")):
        with pytest.raises(Exception, match="bad_argument"):
            db.execute("SELECT ops.record_haproxy_command(%s::uuid, %s, %s, '{}'::jsonb, '{}'::jsonb, %s)", (op, gen, cmd, dump))


# ---------------------------------------------------------------- P6.2-FIX-1: pre-mutation intent, completion, reconcile backfill, LB state push
def real_adapter(fake, pki, tmp_path, push=None):
    return RealAdapter({**REAL_CFG, "haproxy": {"admin_addr": fake.addr, "client_cert": f"{pki}/client.crt", "client_key": f"{pki}/client.key",
                                               "ca_bundle": f"{pki}/ca.crt", "state_dir": str(tmp_path / "st")}}, push=push)


def commands(db, op=None):
    return db.execute("SELECT status, command, dump, backfilled FROM ops.haproxy_commands ORDER BY created_at, id").fetchall()


def test_the_intent_row_is_written_before_the_command_and_completed_with_the_dump(db, fake, pki, tmp_path, monkeypatch):
    op, gen = op_with_lease(db)
    seen = []
    orig = fake._answer

    def spy(cmd):  # at the moment the LB receives the command the intent must already be a committed pending row
        if cmd.startswith("set server"):
            seen.append(commands(db))
        return orig(cmd)

    fake._answer = spy
    ok, _ = routing_action(monkeypatch, real_adapter(fake, pki, tmp_path)).run(op, gen, "lb", "primary_failure")
    assert ok
    assert [[r[0], r[1]] for r in seen[0]] == [["pending", "set server be_app/primary state maint"]]
    done = commands(db)
    assert [(r[0], r[3]) for r in done] == [("applied", False)] and done[0][2] in os.listdir(tmp_path / "st")


def test_audit_failure_after_the_command_leaves_a_pending_row_and_reconcile_backfills_it(db, fake, pki, tmp_path, monkeypatch):
    import routing_bind

    op, gen = op_with_lease(db)
    real_call = routing_bind.call

    def failing(config, sql, params, attempts=3):
        if "complete_haproxy_command" in sql:
            return False, "connect_or_network"
        return real_call(config, sql, params, attempts)

    monkeypatch.setattr(routing_bind, "call", failing)
    adapter = real_adapter(fake, pki, tmp_path)
    assert routing_action(monkeypatch, adapter).run(op, gen, "lb", "primary_failure") == (False, "audit_failed")
    assert fake.servers["primary"][3] == MAINT  # HAProxy is not rolled back: the routing change stands
    assert [(r[0], r[1]) for r in commands(db)] == [("pending", "set server be_app/primary state maint")]
    db.execute("UPDATE ops.haproxy_commands SET created_at = now() - interval '5 minutes'")
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (op, gen))
    out = run_reconcile(db, tmp_path, adapter, monkeypatch)
    assert out["haproxy_backfilled"] == [{"id": out["haproxy_backfilled"][0]["id"], "status": "applied"}]
    row = commands(db)[0]
    assert row[0] == "applied" and row[3] is True and row[2] in os.listdir(tmp_path / "st")
    assert db.execute("SELECT count(*) FROM ops.audit_events WHERE action = 'haproxy.command' AND new_value->>'backfilled' = 'true'").fetchone()[0] == 1


def test_a_pending_row_whose_effect_is_not_observable_is_abandoned_not_applied(db, fake, pki, tmp_path, monkeypatch):
    op, gen = op_with_lease(db)
    db.execute("SELECT ops.begin_haproxy_command(%s::uuid, %s, 'set server be_app/primary state maint\n')", (op, gen))  # intent written, command never sent
    db.execute("UPDATE ops.haproxy_commands SET created_at = now() - interval '5 minutes'")
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (op, gen))
    out = run_reconcile(db, tmp_path, real_adapter(fake, pki, tmp_path), monkeypatch)
    assert [b["status"] for b in out["haproxy_backfilled"]] == ["abandoned"] and commands(db)[0][0] == "abandoned"
    assert fake.mutations() == []  # reconcile never sends a routing command


def test_a_young_pending_row_is_left_to_its_own_worker(db, fake, pki, tmp_path, monkeypatch):
    op, gen = op_with_lease(db)
    db.execute("SELECT ops.begin_haproxy_command(%s::uuid, %s, 'set server be_app/primary state maint\n')", (op, gen))
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (op, gen))
    out = run_reconcile(db, tmp_path, real_adapter(fake, pki, tmp_path), monkeypatch)
    assert "haproxy_backfilled" not in out and commands(db)[0][0] == "pending"


def run_reconcile(db, tmp_path, adapter, monkeypatch):
    import reconcile_pass as rp

    rop, rgen = op_with_lease(db, "reconcile")
    a = rp.ReconcilePass.__new__(rp.ReconcilePass)
    a.config = {"supabase_ops_url": login_url()}
    adapter.list_by_tag = lambda provider, tags: {"state": "unavailable", "code": "provider_error"}
    monkeypatch.setattr(rp.providers, "load", lambda cfg: adapter)
    a._api = lambda: (_ for _ in ()).throw(rp.St2Unavailable("st2_api_unavailable"))
    ok, out = a.run(rop, rgen)
    assert ok, out
    db.execute("SELECT ops.release_lease(%s::uuid, %s)", (rop, rgen))
    return out


def test_an_intent_that_cannot_be_written_sends_nothing(db, fake, pki, tmp_path, monkeypatch):
    import routing_bind

    op, gen = op_with_lease(db)
    real_call = routing_bind.call
    monkeypatch.setattr(routing_bind, "call", lambda c, sql, p, **k: (False, "connect_or_network") if "begin_haproxy_command" in sql else real_call(c, sql, p, **k))
    assert routing_action(monkeypatch, real_adapter(fake, pki, tmp_path)).run(op, gen, "lb", "primary_failure") == (False, "intent_failed")
    assert fake.mutations() == [] and commands(db) == []


def test_a_fenced_worker_writes_no_intent_and_sends_nothing(db, fake, pki, tmp_path, monkeypatch):
    op, gen = op_with_lease(db)
    assert routing_action(monkeypatch, real_adapter(fake, pki, tmp_path)).run(op, gen + 1, "lb", "primary_failure") == (False, "lease_lost")
    assert fake.mutations() == [] and commands(db) == []


def test_slot_projection_is_committed_after_haproxy_and_not_at_all_when_haproxy_fails(db, sim, fake, pki, tmp_path, monkeypatch):
    from test_real_adapter import bound_real_instance

    op, gen, iid = bound_real_instance(db, sim)
    key, sgen = db.execute("SELECT key, generation FROM ops.capacity_slots WHERE bound_instance_id = %s::uuid", (iid,)).fetchone()
    db.execute("UPDATE ops.instances SET lifecycle_state = 'ready', public_ip = '203.0.113.9' WHERE id = %s::uuid", (iid,))
    adapter = real_adapter(fake, pki, tmp_path)
    mode = lambda: db.execute("SELECT routing_mode FROM ops.instances WHERE id = %s::uuid", (iid,)).fetchone()[0]  # noqa: E731
    before = mode()
    fake.overrides["set server"] = "Unknown command\n"
    code = routing_action(monkeypatch, adapter).run(op, gen, key, "active", slot_generation=sgen)
    assert code[0] is False and mode() == before  # HAProxy refused: the projection never claims the routing
    fake.overrides.clear()
    ok, _ = routing_action(monkeypatch, adapter).run(op, gen, key, "active", slot_generation=sgen)
    assert ok and mode() == "active"


# ---- LB state push
class Runs:
    def __init__(self, rc=(0, 0)):
        self.calls, self.rc, self.files = [], list(rc), []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] == "scp":
            self.files.append((open(argv[-2]).read(), oct(os.stat(argv[-2]).st_mode & 0o777)))
        return self.rc[len(self.calls) - 1]


def test_lb_state_push_copies_to_the_fixed_destination_then_installs_it_0600_atomically():
    runs = Runs()
    hx.LbStatePush("203.0.113.5", "root", "/k/id", "/k/known", run=runs)("1\n# state\n")
    scp, ssh = runs.calls
    assert scp[0] == "scp" and scp[-1] == "root@203.0.113.5:/opt/drproj/lb/state/server-state.push" and runs.files == [("1\n# state\n", "0o600")]
    assert ssh[0] == "ssh" and ssh[-2] == "root@203.0.113.5" and ssh[-1] == (
        "chown 99:99 /opt/drproj/lb/state/server-state.push && chmod 0600 /opt/drproj/lb/state/server-state.push && "
        "mv -f /opt/drproj/lb/state/server-state.push /opt/drproj/lb/state/server-state")
    assert "StrictHostKeyChecking=yes" in scp and "BatchMode=yes" in scp
    for bad in (("203.0.113.5; id", "root"), ("203.0.113.5", "root x")):
        with pytest.raises(AdminError, match="lb_ssh_config_invalid"):
            hx.LbStatePush(bad[0], bad[1], "/k", run=runs)("x")
    for rc, code in (((1, 0), "lb_state_copy_failed"), ((0, 1), "lb_state_install_failed")):
        with pytest.raises(AdminError, match=code):
            hx.LbStatePush("203.0.113.5", "root", "/k", run=Runs(rc))("x")


def test_every_dump_is_pushed_in_order_and_a_failed_push_neither_stops_the_plan_nor_is_silent(fake, pki, tmp_path, caplog):
    pushed = []
    hooks = Hooks()
    a = adapter_for(fake, pki, tmp_path, hooks)
    a.push = lambda text: pushed.append(text)
    assert a.set_routing("lb", "overflow") == 2
    stored = [(tmp_path / "store" / d).read_text() for d in sorted(os.listdir(tmp_path / "store"))]
    assert pushed == stored and hx.parse_state(pushed[-1]).get("warm_act").is_ready

    def boom(text):
        raise AdminError("lb_state_copy_failed")

    b = adapter_for(fake, pki, tmp_path / "b", Hooks())
    b.push = boom
    with caplog.at_level("WARNING"):
        assert b.set_routing("lb", "primary_failure") == 1
    assert fake.servers["primary"][3] == MAINT and "lb state sync failed code=lb_state_copy_failed" in caplog.text


def test_the_dump_reaches_the_lb_even_when_the_audit_completion_fails(fake, pki, tmp_path):
    pushed = []
    a = adapter_for(fake, pki, tmp_path, Hooks())
    a.push = pushed.append

    def bad_audit(*args):
        raise AdminError("audit_failed")

    a.bind(Hooks().fence, bad_audit)
    with pytest.raises(AdminError, match="audit_failed"):
        a.set_routing("lb", "primary_failure")
    assert len(pushed) == 1 and hx.parse_state(pushed[0]).get("primary").is_maint
