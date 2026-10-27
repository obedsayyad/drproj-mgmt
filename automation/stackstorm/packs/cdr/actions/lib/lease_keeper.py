"""In-process lease heartbeat for long actions (docs/07).

A helper thread renews the lease every `interval` seconds (policy `lease_renewal_seconds`). After
`max_failures` consecutive failed renewals (2 by default, 40 s with the default interval) it sets the stop flag:
the action must start no new side effect and exit with `lease_lost`. A renewal that the database rejects as
lease_lost (fenced or expired) is definitive and stops at once. The lease expiry and the fencing generation stay
the safety net for a worker that did not notice.
"""
import threading

from cdr_db import call


class LeaseLost(Exception):
    """The lease is no longer ours (or cannot be shown to be). No further side effect may run."""


def _renew_via_db(config, op, gen, app_key, ttl):
    ok, out = call(config, "SELECT ops.renew_lease(%s::uuid, %s, %s, %s)", (op, gen, app_key, ttl), attempts=1)
    return (True, None) if ok else (False, out)


class LeaseKeeper:
    def __init__(self, config, operation_id, generation, interval, app_key="relief", ttl_seconds=120, max_failures=2, renew=None):
        self._renew = renew or (lambda: _renew_via_db(config, operation_id, generation, app_key, ttl_seconds))
        self.interval = interval
        self.max_failures = max_failures
        self.failures = 0
        self.stopped = threading.Event()  # set = lease considered lost
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="lease-keeper", daemon=True)

    def _loop(self):
        while not self._done.wait(self.interval):
            self.renew_once()
            if self.stopped.is_set():
                return

    def renew_once(self):
        ok, code = self._renew()
        if ok:
            self.failures = 0
            return True
        self.failures += 1
        if code == "lease_lost" or self.failures >= self.max_failures:
            self.stopped.set()
        return False

    def check(self):
        """Call before every side effect."""
        if self.stopped.is_set():
            raise LeaseLost("lease_lost")

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._done.set()
        self._thread.join(timeout=5)
        return False


def fenced(config, operation_id, generation, fn, keeper=None, app_key="relief"):
    """assert_lease immediately before the external call, inside the same action, then run it."""
    if keeper is not None:
        keeper.check()
    ok, out = call(config, "SELECT ops.assert_lease(%s::uuid, %s, %s)", (operation_id, generation, app_key))
    if not ok:
        raise LeaseLost(out) if out == "lease_lost" else RuntimeError(out)
    return fn()
