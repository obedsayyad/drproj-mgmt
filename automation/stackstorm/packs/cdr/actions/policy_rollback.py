"""apply_policy failure (docs/03 activation 7): restore the previous label and reload (best effort, reported), then mark the pending
version `failed`. The old version stays active in every case. Idempotent: an already finished version is not an error."""
import json

from cdr_common import failure_code, guarded, policy_versions
from cdr_db import call
from prom_render import read_policy_version, set_policy_version
from prom_target import load
from st2common.runners.base_action import Action


class PolicyRollback(Action):
    @guarded
    def run(self, operation_id, error=""):
        ok, v = policy_versions(self.config, operation_id)
        if not ok:
            return False, v
        restored = False
        touched = False
        try:
            target = load(self.config)
            current = target.read()
            if read_policy_version(current) == str(v["from_version"]):
                restored = True  # nothing was applied (validation failed first): Prometheus is not touched
            else:
                touched = True
                target.write(set_policy_version(current, v["from_version"]))
                restored = target.reload()
        except Exception:  # noqa: BLE001 - best effort; the report says whether it worked
            restored = False
        failed = False
        if v["pending_status"] == "pending":
            ok, out = call(self.config, "SELECT ops.finish_policy_apply(%s::uuid, %s, false, %s::jsonb)", (operation_id, v["version"], json.dumps({"error": failure_code(error, "failed"), "label_restored": restored})))
            if not ok:
                return False, out
            failed = True
        return True, {"label_restored": restored, "prometheus_touched": touched, "version_marked_failed": failed, "active_version": v["from_version"]}
