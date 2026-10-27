"""Single failure handler for the capacity workflows; idempotent, so the workflow may retry it.

lease_lost (this worker was fenced) must not touch the lease and leaves the operation `reconciling` for the reconcile
workflow. Any other error fails the operation and then releases the lease. An operation that is already terminal
(the failure came after it was recorded, for example in the release task) is left as it is, but the lease is still
released. The task fails (so the workflow retries it) only when the database cannot be reached: a released=false
answer means the lease was not ours (fenced or already released), which is final. Slots held by the failed run stay
counted until reconcile resolves them."""
import json

from cdr_common import advance, failure_code
from cdr_db import call
from st2common.runners.base_action import Action

TERMINAL_ALREADY = "invalid_transition"  # advance_operation refuses to rewrite a terminal operation


class FailOperation(Action):
    def run(self, operation_id, lease_generation=0, error=None, step="failed", app_key="relief"):
        text = json.dumps(error, default=str) if error is not None else ""
        if "lease_lost" in text:
            ok, out = advance(self.config, operation_id, "operation.step.completed", step, "reconciling", "lease lost; reconcile will resolve held resources")
            if not ok and out != TERMINAL_ALREADY:
                return False, out
            return True, {"status": "reconciling"}
        code = failure_code(text)
        outcome = {"error": code, "step": step}
        if isinstance(error, dict) and error.get("detail") is not None and len(json.dumps(error["detail"], default=str)) < 1500:
            outcome["detail"] = error["detail"]
        ok, out = advance(self.config, operation_id, "operation.failed", step, "failed", "not implemented in this phase" if code == "not_implemented" else "workflow error", outcome)
        if not ok and out != TERMINAL_ALREADY:
            return False, out
        released = False
        if lease_generation:
            ok, rows = call(self.config, "SELECT ops.release_lease(%s::uuid, %s, %s)", (operation_id, lease_generation, app_key))
            if not ok:
                return False, rows
            released = bool(rows[0][0])
        return True, {"status": "failed", "released": released}
