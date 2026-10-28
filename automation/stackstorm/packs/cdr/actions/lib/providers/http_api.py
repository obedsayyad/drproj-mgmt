"""DigitalOcean and Linode HTTP reads and power-on for the real adapter (docs/07, "Real adapter").

Every call answers with an explicit code and never raises for a network or HTTP problem:
  get          404 -> absent; 200 -> present; anything else (timeout, 429, 5xx, 401/403) -> unknown
  list_by_tag  the provider is asked for project:cdr, then a resource counts only when ALL requested tags match; any failure -> unknown
  observe      200 -> available; 401/403/429 -> unavailable; anything else -> unknown
  power_on     2xx -> ok; 401/403/429 -> unavailable; anything else -> unknown
A provider whose token variable is unset is unconfigured: no HTTP call is made and every read answers unavailable with
code provider_unconfigured (observe: "unavailable"). It never raises and is never absence.
The token is read from the environment at call time, sent only in the Authorization header, and never logged or returned.
"""
import json
import os

import requests

from .base import AdapterConfigError, PROJECT_TAG, now_iso

TIMEOUT_S = 10
MAX_PAGES = 20
REFUSED = (401, 403, 429)
UNCONFIGURED = "provider_unconfigured"


def token_from_ref(ref):
    """`env://NAME` or `NAME` -> the value of that environment variable. A missing one is a configuration error (code only)."""
    name = (ref or "").removeprefix("env://")
    value = os.environ.get(name) if name else None
    if not value:
        raise AdapterConfigError("real_token_missing")
    return value


def token_configured(ref):
    name = (ref or "").removeprefix("env://")
    return bool(name and os.environ.get(name))


def tags_to_dict(tags):
    out = {}
    for t in tags or []:
        k, sep, v = str(t).partition(":")
        if sep:
            out[k] = v
    return out


class _Http:
    base = ""

    def __init__(self, token_ref, session=None):
        self._token_ref = token_ref
        self._session = session or requests.Session()

    def _request(self, method, path, **kw):
        headers = {"Authorization": "Bearer " + token_from_ref(self._token_ref), **kw.pop("headers", {})}
        try:
            return self._session.request(method, self.base + path, headers=headers, timeout=TIMEOUT_S, **kw)
        except requests.RequestException:
            return None

    def configured(self):
        return token_configured(self._token_ref)

    def observe(self):
        if not self.configured():
            return "unavailable"
        r = self._request("GET", self.account_path)
        if r is None:
            return "unknown"
        return "available" if r.status_code == 200 else "unavailable" if r.status_code in REFUSED else "unknown"

    def get(self, resource_id):
        if not self.configured():
            return {"state": "unavailable", "code": UNCONFIGURED, "observed_at": now_iso()}
        r = self._request("GET", self.item_path.format(id=resource_id))
        if r is None:
            return {"state": "unknown", "observed_at": now_iso()}
        if r.status_code == 404:
            return {"state": "absent", "observed_at": now_iso()}
        if r.status_code != 200:
            return {"state": "unknown", "observed_at": now_iso()}
        try:
            return {"state": "present", "observed_at": now_iso(), **self._summary(r.json())}
        except (ValueError, KeyError, TypeError, AttributeError):
            return {"state": "unknown", "observed_at": now_iso()}

    def list_by_tag(self, tags):
        if not self.configured():
            return {"state": "unavailable", "code": UNCONFIGURED, "resources": []}
        found = []
        for page in range(1, MAX_PAGES + 1):
            r = self._request("GET", **self._list_args(page))
            if r is None or r.status_code != 200:
                return {"state": "unknown", "resources": []}
            try:
                body = r.json()
                for item in self._items(body):
                    s = self._summary(item)
                    if all(s["tags"].get(k) == v for k, v in tags.items()):
                        found.append({"resource_id": s["resource_id"], "tags": s["tags"], "ip": s["ip"]})
                if not self._has_more(body, page):
                    return {"state": "ok", "resources": found}
            except (ValueError, KeyError, TypeError, AttributeError):
                return {"state": "unknown", "resources": []}
        return {"state": "unknown", "resources": []}  # more pages than we are willing to read: no answer, not "complete"

    def power_on(self, resource_id):
        if not self.configured():
            return {"result": "unavailable", "code": UNCONFIGURED}
        r = self._request("POST", **self._power_args(resource_id))
        if r is None:
            return {"result": "unknown"}
        if 200 <= r.status_code < 300:
            return {"result": "ok"}
        return {"result": "unavailable" if r.status_code in REFUSED else "unknown"}


class DigitalOceanHttp(_Http):
    base = "https://api.digitalocean.com"
    account_path = "/v2/account"
    item_path = "/v2/droplets/{id}"

    def _summary(self, body):
        d = body.get("droplet", body)
        ip = next((n["ip_address"] for n in (d.get("networks") or {}).get("v4", []) if n.get("type") == "public"), None)
        return {"resource_id": str(d["id"]), "power": "off" if d.get("status") == "off" else "on", "ip": ip,
                "vcpu": d.get("vcpus"), "memory_mb": d.get("memory"), "tags": tags_to_dict(d.get("tags"))}

    def _list_args(self, page):
        return {"path": f"/v2/droplets?tag_name={PROJECT_TAG_Q}&per_page=200&page={page}"}

    def _items(self, body):
        return body.get("droplets", [])

    def _has_more(self, body, page):
        return bool(((body.get("links") or {}).get("pages") or {}).get("next"))

    def _power_args(self, rid):
        return {"path": f"/v2/droplets/{rid}/actions", "json": {"type": "power_on"}}


class LinodeHttp(_Http):
    base = "https://api.linode.com"
    account_path = "/v4/profile"
    item_path = "/v4/linode/instances/{id}"

    def _summary(self, body):
        specs = body.get("specs") or {}
        return {"resource_id": str(body["id"]), "power": "off" if body.get("status") == "offline" else "on", "ip": (body.get("ipv4") or [None])[0],
                "vcpu": specs.get("vcpus"), "memory_mb": specs.get("memory"), "tags": tags_to_dict(body.get("tags"))}

    def _list_args(self, page):
        return {"path": f"/v4/linode/instances?page={page}&page_size=100", "headers": {"X-Filter": json.dumps({"tags": PROJECT_TAG_Q})}}

    def _items(self, body):
        return body.get("data", [])

    def _has_more(self, body, page):
        return page < int(body.get("pages", 1))

    def _power_args(self, rid):
        return {"path": f"/v4/linode/instances/{rid}/boot", "json": {}}


PROJECT_TAG_Q = "project:" + PROJECT_TAG["project"]
