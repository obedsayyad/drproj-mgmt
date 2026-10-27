"""Minimal StackStorm API client for actions (execution lookup, cancel, dispatch).

The python-script runner injects ST2_ACTION_API_URL and ST2_ACTION_AUTH_TOKEN (a token for the running execution's
user), so no API key is kept in the pack config. Failures raise St2Unavailable; callers must treat that as "unknown",
never as "the execution is gone".
"""
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

LIVE_STATUSES = frozenset({"requested", "scheduled", "delayed", "running", "pausing", "paused", "resuming", "canceling"})
TIMEOUT_S = 30
DISPATCH_ATTEMPTS = 3
DISPATCH_BACKOFF_S = 2
LOG = logging.getLogger(__name__)


class St2Unavailable(Exception):
    """The StackStorm API did not answer usefully."""


class St2Api:
    def __init__(self, base=None, token=None):
        base = (base or os.environ.get("ST2_ACTION_API_URL") or "").rstrip("/")
        if not base:
            raise St2Unavailable("st2_api_url_missing")
        self.base = base if base.endswith("/v1") else base + "/v1"
        self.token = token or os.environ.get("ST2_ACTION_AUTH_TOKEN") or ""

    def _req(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Auth-Token": self.token, "Content-Type": "application/json"})
        started = time.monotonic()
        outcome = "ok"
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310 - the st2 API of this stack
                return json.load(r)
        except urllib.error.HTTPError as exc:
            outcome = f"http_{exc.code}"
            if exc.code == 404:
                return None
            raise St2Unavailable(outcome) from None
        except Exception as exc:  # noqa: BLE001 - code only
            outcome = type(exc).__name__[:40]
            raise St2Unavailable(outcome) from None
        finally:
            LOG.info("st2api %s %s outcome=%s latency_ms=%d", method, path.split("?")[0], outcome, int((time.monotonic() - started) * 1000))

    def execution(self, execution_id):
        """The execution document, or None when st2 does not know the id."""
        return self._req("GET", "/executions/" + urllib.parse.quote(str(execution_id), safe=""))

    def find_live(self, action_ref, operation_id):
        """Executions of `action_ref` whose operation_id parameter matches and whose status is live (newest 100 are scanned)."""
        q = urllib.parse.urlencode({"action": action_ref, "limit": 100, "include_attributes": "id,status,parameters"})
        rows = self._req("GET", "/executions?" + q) or []
        return [r for r in rows if (r.get("parameters") or {}).get("operation_id") == str(operation_id) and r.get("status") in LIVE_STATUSES]

    def _landed(self, action_ref, parameters):
        """The id of a live execution this dispatch already created (matched by operation_id), else None."""
        op = (parameters or {}).get("operation_id")
        if op is None:
            return None
        try:
            live = self.find_live(action_ref, op)
        except St2Unavailable:
            return None
        return live[0]["id"] if live else None

    def cancel(self, execution_id):
        return self._req("PUT", "/executions/" + urllib.parse.quote(str(execution_id), safe=""), {"status": "canceled"})

    def run(self, action_ref, parameters):
        """Dispatch asynchronously; returns the new execution id."""
        doc = None
        for n in range(DISPATCH_ATTEMPTS):
            try:
                doc = self._req("POST", "/executions", {"action": action_ref, "parameters": parameters})
                break
            except St2Unavailable as exc:
                # 5xx: st2 did not accept it. Timeout: the POST may have landed, so look for the live execution first.
                if not _retryable(str(exc)) or n + 1 >= DISPATCH_ATTEMPTS:
                    raise
                if _maybe_landed(str(exc)):
                    live = self._landed(action_ref, parameters)
                    if live:
                        return live
                time.sleep(DISPATCH_BACKOFF_S * (n + 1))
        if not doc or "id" not in doc:
            raise St2Unavailable("dispatch_no_id")
        return doc["id"]


def _retryable(code):
    return code.startswith("http_5") or code in ("timeout", "TimeoutError", "URLError", "ConnectionError", "RemoteDisconnected")


def _maybe_landed(code):
    """A timeout (no response) may hide a POST that succeeded; a 5xx response means st2 did not accept it."""
    return not code.startswith("http_")
