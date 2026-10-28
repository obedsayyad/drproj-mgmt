"""HAProxy Runtime API client for the real routing adapter (docs/08 fe_admin, docs/07).

fe_admin is mTLS (:9999, client CN drproj-runner) and permits ONE command per connection. This client mirrors the LB's
admin_allow.regex and refuses before connecting, so a disallowed command never reaches the socket. Every answer is checked:
an unknown or malformed answer is an AdminError, never assumed success. Mutations are followed by a `show servers state`
dump saved to the versioned membership store (docs/08) and recorded in ops.audit_events by the caller.
"""
import os
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone

TIMEOUT = 5
MAX_WEIGHT = 256
BACKEND = "be_app"

# Mirror of infra/ansible/roles/lb/files/haproxy/admin_allow.regex (tests assert byte-for-byte parity).
ALLOW_PATTERNS = (
    r"^show (info|stat)\n$",
    r"^show servers (state|conn)( be_app)?\n$",
    r"^set server be_app/[A-Za-z0-9_-]{1,32} state (ready|drain|maint)\n$",
    r"^set server be_app/[A-Za-z0-9_-]{1,32} weight [0-9]{1,3}%?\n$",
    r"^set server be_app/(warm_bk|warm_act|iwarm_bk|iwarm_act|elastic-(do|linode)-[1-5]) addr [0-9]{1,3}(\.[0-9]{1,3}){3} port [0-9]{1,5}\n$",
)
_ALLOW = tuple(re.compile(p) for p in ALLOW_PATTERNS)
_WEIGHT = re.compile(r" weight ([0-9]{1,3})(%?)\n$")
_ADDR = re.compile(r" addr ([0-9.]+) port ([0-9]+)\n$")

# HAProxy server admin-state bits (show servers state, srv_admin_state).
_ADM_MAINT = 0x01 | 0x02 | 0x04 | 0x20 | 0x40
_ADM_DRAIN = 0x08 | 0x10


class AdminError(Exception):
    """The Runtime API did not give a clear, expected answer. Carries a short code, never socket text."""


class CommandRefused(AdminError):
    """Refused by the client allowlist before anything was sent."""


class ParseError(AdminError):
    pass


def check(cmd):
    """Raise CommandRefused unless cmd (with trailing newline) is allowed. Stricter than the LB: weight <= 256, addr valid, never on primary."""
    if not isinstance(cmd, str) or not cmd.endswith("\n") or cmd.count("\n") != 1:
        raise CommandRefused("command_refused")
    if not any(p.match(cmd) for p in _ALLOW):
        raise CommandRefused("command_refused")
    m = _WEIGHT.search(cmd)
    if m and int(m.group(1)) > MAX_WEIGHT and not m.group(2):
        raise CommandRefused("command_refused")
    m = _ADDR.search(cmd)
    if m:
        octets = m.group(1).split(".")
        if len(octets) != 4 or any(int(o) > 255 for o in octets) or not 1 <= int(m.group(2)) <= 65535:
            raise CommandRefused("command_refused")
    return cmd


@dataclass(frozen=True)
class Server:
    name: str
    addr: str
    port: int
    op_state: int
    admin_state: int
    weight: int

    @property
    def is_up(self):
        return self.op_state == 2 and not (self.admin_state & (_ADM_MAINT | _ADM_DRAIN))

    @property
    def is_maint(self):
        return bool(self.admin_state & _ADM_MAINT)

    @property
    def is_drain(self):
        return bool(self.admin_state & _ADM_DRAIN) and not self.is_maint

    @property
    def is_ready(self):
        return not (self.admin_state & (_ADM_MAINT | _ADM_DRAIN))


@dataclass(frozen=True)
class Membership:
    servers: dict

    def get(self, name):
        if name not in self.servers:
            raise AdminError("server_unknown")
        return self.servers[name]


def parse_state(text):
    """Parse `show servers state` (format version 1) into a Membership of be_app servers. Any deviation is a ParseError."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2 or lines[0].strip() != "1" or not lines[1].startswith("#"):
        raise ParseError("state_header_invalid")
    cols = lines[1].lstrip("# ").split()
    try:
        idx = {c: cols.index(c) for c in ("be_name", "srv_name", "srv_addr", "srv_op_state", "srv_admin_state", "srv_uweight", "srv_port")}
    except ValueError:
        raise ParseError("state_columns_missing")
    servers = {}
    for ln in lines[2:]:
        f = ln.split()
        if len(f) < len(cols):
            raise ParseError("state_row_short")
        if f[idx["be_name"]] != BACKEND:
            continue
        try:
            s = Server(f[idx["srv_name"]], f[idx["srv_addr"]], int(f[idx["srv_port"]]), int(f[idx["srv_op_state"]]),
                       int(f[idx["srv_admin_state"]]), int(f[idx["srv_uweight"]]))
        except ValueError:
            raise ParseError("state_row_invalid")
        servers[s.name] = s
    if not servers:
        raise ParseError("state_no_servers")
    return Membership(servers)


def parse_conn(text):
    """`show servers conn` -> {server name: current connections}. Two layouts are accepted, anything else is a ParseError (never "0"):
      HAProxy 3.x   `# bkname/svname bkid/svid addr port - purge_delay used_cur used_max ...`   rows `be_app/primary 3/1 ...` (used_cur)
      column-named  `# be_id be_name srv_id srv_name ... srv_cur_sess`                           (srv_cur_sess)"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    head = next((ln for ln in lines if ln.startswith("#")), None)
    if head is None:
        raise ParseError("conn_header_missing")
    cols = head.lstrip("# ").split()
    if "bkname/svname" in cols and "used_cur" in cols:
        i_key, i_cur, split = cols.index("bkname/svname"), cols.index("used_cur"), True
    elif {"be_name", "srv_name", "srv_cur_sess"} <= set(cols):
        i_be, i_key, i_cur, split = cols.index("be_name"), cols.index("srv_name"), cols.index("srv_cur_sess"), False
    else:
        raise ParseError("conn_columns_missing")
    out = {}
    for ln in lines:
        if ln.startswith("#"):
            continue
        f = ln.split()
        if len(f) < len(cols):
            raise ParseError("conn_row_short")
        if split:
            backend, _, name = f[i_key].partition("/")
        else:
            backend, name = f[i_be], f[i_key]
        if backend != BACKEND:
            continue
        try:
            out[name] = int(f[i_cur])
        except ValueError:
            raise ParseError("conn_row_invalid")
    if not out:
        raise ParseError("conn_no_servers")
    return out


class AdminClient:
    """One TLS connection per command. `connect` is injectable for tests; the default dials addr ("host:port") with the runner client cert."""

    def __init__(self, addr, cert, key, ca, timeout=TIMEOUT, connect=None):
        self.addr, self.cert, self.key, self.ca, self.timeout = addr, cert, key, ca, timeout
        self._connect = connect or self._dial

    def _dial(self):
        host, _, port = self.addr.rpartition(":")
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=self.ca)
        ctx.load_cert_chain(self.cert, self.key)
        raw = socket.create_connection((host, int(port)), timeout=self.timeout)
        try:
            return ctx.wrap_socket(raw, server_hostname=host)
        except Exception:
            raw.close()
            raise

    def send(self, cmd):
        check(cmd)  # fail closed before any connection
        try:
            conn = self._connect()
            try:
                conn.settimeout(self.timeout)
                conn.sendall(cmd.encode("ascii"))
                chunks = []
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
            finally:
                conn.close()
        except (OSError, ssl.SSLError):  # includes socket.timeout
            raise AdminError("admin_unreachable")
        return b"".join(chunks).decode("utf-8", "replace")

    def show_state(self):
        text = self.send("show servers state\n")
        return text, parse_state(text)

    def show_conn(self):
        return parse_conn(self.send("show servers conn\n"))

    def set_cmd(self, cmd):
        """A `set server` command. HAProxy answers an empty body on success; any text is an error or an unknown answer."""
        if not cmd.startswith("set server "):
            raise CommandRefused("command_refused")
        answer = self.send(cmd).strip()
        if answer:
            raise AdminError("admin_answer_unexpected")


class MembershipStore:
    """Versioned `show servers state` dumps on the management VM (dir 0700, files 0600): <UTC ts>.state."""

    def __init__(self, directory):
        self.dir = directory

    def save(self, text):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        os.chmod(self.dir, 0o700)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        name = ts + ".state"
        fd = os.open(os.path.join(self.dir, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        return name

    def latest_valid(self, expected):
        """Newest dump that parses and contains every expected be_app server line; None when none validates."""
        if not os.path.isdir(self.dir):
            return None
        for name in sorted((n for n in os.listdir(self.dir) if n.endswith(".state")), reverse=True):
            try:
                with open(os.path.join(self.dir, name)) as fh:
                    text = fh.read()
                if set(expected) <= set(parse_state(text).servers):
                    return name, text
            except (OSError, AdminError):
                continue
        return None
