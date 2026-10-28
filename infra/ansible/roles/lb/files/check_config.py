#!/usr/bin/env python3
"""Policy checks on haproxy.cfg that `haproxy -c` does not make (docs/08). Usage: check_config.py <haproxy.cfg>

- `stats socket` must be a unix socket: `stats socket ... ssl` or a TCP address is accepted by `haproxy -c` but is plaintext/unauthenticated.
- Only the known listeners may bind; every TLS bind other than the public app (443) and basic-auth stats (8404) must say `verify required` (mTLS).
- No bind may use `verify optional` or `verify none`.
"""
import re
import sys

PUBLIC_TLS_PORTS = {"443", "8404"}
ALLOWED_PORTS = {"80", "443", "8404", "9999"}


def problems(cfg: str) -> list[str]:
    out = []
    for n, raw in enumerate(cfg.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if m := re.match(r"stats\s+socket\s+(\S+)", line):
            if not re.match(r"(/|unix@|abns@)", m.group(1)):
                out.append(f"line {n}: stats socket must be a unix socket, got {m.group(1)}")
        if m := re.match(r"bind\s+(\S+)(.*)", line):
            addr, rest = m.groups()
            port = addr.rsplit(":", 1)[-1]
            tokens = rest.split()
            if port not in ALLOWED_PORTS:
                out.append(f"line {n}: bind {addr}: port not in the allowed listener set {sorted(ALLOWED_PORTS)}")
            if re.search(r"verify\s+(optional|none)", rest):
                out.append(f"line {n}: bind {addr}: verify optional/none is not allowed")
            if "ssl" in tokens and port not in PUBLIC_TLS_PORTS and not re.search(r"verify\s+required", rest):
                out.append(f"line {n}: bind {addr}: ssl bind lacks `verify required`")
    return out


if __name__ == "__main__":
    found = problems(open(sys.argv[1]).read())
    for p in found:
        print(p, file=sys.stderr)
    sys.exit(1 if found else 0)
