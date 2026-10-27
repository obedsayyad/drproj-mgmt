"""Wait for the capacity lease (docs/07: operations queue behind the holder; recheck happens after the wait).

Polls ops.acquire_lease. An expired lease of another operation (slot_conflict) is not ours to take: only reconcile
does, so keep waiting. Ends early, without the lease, when the operation became terminal meanwhile (superseded by a
failure-triggered intent). Emits operation.waiting every ~30 s so the operation shows life while queued.
"""
import time

from cdr_common import advance, guarded, policy_section
from cdr_db import call
from st2common.runners.base_action import Action

TERMINAL = ("succeeded", "failed", "cancelled")
EVENT_EVERY_S = 30


class WaitLease(Action):
    @guarded
    def run(self, operation_id, app_key="relief", timeout_seconds=420, poll_seconds=5):
        ok, coordination = policy_section(self.config, operation_id, "coordination")
        ttl = coordination["lease_seconds"] if ok else 120
        end = time.monotonic() + timeout_seconds
        next_event = time.monotonic() + EVENT_EVERY_S
        while True:
            ok, rows = call(self.config, "SELECT generation, previous_holder::text, previous_expired FROM ops.acquire_lease(%s::uuid, %s, %s)", (operation_id, app_key, ttl))
            if ok and rows:
                generation, previous_holder, previous_expired = rows[0]
                return True, {"acquired": True, "generation": generation, "previous_holder": previous_holder, "previous_expired": previous_expired}
            if not ok and out_is_fatal(rows):
                return False, rows
            ok2, st = call(self.config, "SELECT status FROM ops.operations WHERE id = %s::uuid", (operation_id,))
            if ok2 and st and st[0][0] in TERMINAL:
                return True, {"acquired": False, "reason": "superseded"}
            if time.monotonic() >= end:
                return True, {"acquired": False, "reason": "timeout"}
            if time.monotonic() >= next_event:
                next_event = time.monotonic() + EVENT_EVERY_S
                ok3, out = advance(self.config, operation_id, "operation.waiting", "wait_lease", "waiting", "waiting for the capacity lease")
                if not ok3 and out != "invalid_transition":
                    return False, out
            time.sleep(poll_seconds)


def out_is_fatal(code):
    # slot_conflict: another operation's expired lease (reconcile only); bad_argument: the operation is no longer active
    # (checked against its status next); connect_or_network: retry on the next poll. Anything else is a real failure.
    return code not in ("slot_conflict", "bad_argument", "connect_or_network")
