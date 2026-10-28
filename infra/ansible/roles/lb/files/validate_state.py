#!/usr/bin/env python3
"""Pick the newest VALID `show servers state` dump from the membership store (docs/08): a dump is valid when it parses
(format version 1, be_app rows) and every expected be_app server line is present. Prints the file name and exits 0; exits 1
when no dump validates, so the role falls back to the dump taken from the live LB. Standalone (stdlib only): it runs on management."""
import argparse
import os
import sys


def servers(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2 or lines[0].strip() != "1" or not lines[1].startswith("#"):
        return None
    cols = lines[1].lstrip("# ").split()
    if "be_name" not in cols or "srv_name" not in cols:
        return None
    i_be, i_srv = cols.index("be_name"), cols.index("srv_name")
    found = set()
    for ln in lines[2:]:
        f = ln.split()
        if len(f) < len(cols):
            return None
        if f[i_be] == "be_app":
            found.add(f[i_srv])
    return found


def valid(text, expected):
    found = servers(text)
    return found is not None and set(expected) <= found


def newest_valid(directory, expected):
    if not os.path.isdir(directory):
        return None
    for name in sorted((n for n in os.listdir(directory) if n.endswith(".state")), reverse=True):
        try:
            with open(os.path.join(directory, name)) as fh:
                if valid(fh.read(), expected):
                    return name
        except OSError:
            continue
    return None


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--expected", required=True, help="comma-separated be_app server names that must all be present")
    args = ap.parse_args(argv)
    name = newest_valid(args.dir, [e for e in args.expected.split(",") if e])
    if name is None:
        return 1
    print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
