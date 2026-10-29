#!/usr/bin/env python3
"""Print "<City>, <Country>" for a public IP (one GET to ipinfo.io), or nothing when it cannot be resolved. Never guesses; always exits 0."""
import json
import re
import sys
import urllib.request

SAFE = re.compile(r"^[\w ().,-]{1,38}$")  # subset of the relief app's INSTANCE_REGION pattern


def build_label(info):
    """City + country from an ipinfo.io response; '' unless both are present and plain text."""
    if not isinstance(info, dict):
        return ""
    city, country = (str(info.get(k) or "").strip() for k in ("city", "country"))
    if not (SAFE.match(city) and SAFE.match(country)):
        return ""
    return f"{city}, {country}"


def lookup(ip, fetch=None):
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip or ""):
        return ""
    try:
        if fetch is None:
            with urllib.request.urlopen(f"https://ipinfo.io/{ip}/json", timeout=5) as r:  # noqa: S310 fixed https host
                return build_label(json.load(r))
        return build_label(fetch(ip))
    except Exception:  # network, HTTP, JSON: all mean "unknown"
        return ""


if __name__ == "__main__":
    print(lookup(sys.argv[1] if len(sys.argv) > 1 else ""))
