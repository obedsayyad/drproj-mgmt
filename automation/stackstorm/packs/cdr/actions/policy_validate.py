"""apply_policy step 1 (docs/03 activation 1-3): the pending version of this operation, its bounds, and what it touches.
Read-only. ok=false carries the errors; `unimplemented` lists changed settings whose compile target (Prometheus rule thresholds, HAProxy,
k6, StackStorm timers) is a later phase, which the workflow refuses with not_implemented instead of activating."""
from cdr_common import guarded, policy_versions
from policy_scope import classify, diff, validate
from st2common.runners.base_action import Action


class PolicyValidate(Action):
    @guarded
    def run(self, operation_id):
        ok, v = policy_versions(self.config, operation_id)
        if not ok:
            return False, v
        if v["pending_status"] != "pending":
            return True, {"ok": False, "errors": [f"version {v['version']} is {v['pending_status']}, not pending"], "version": v["version"], "from_version": v["from_version"]}
        errors = validate(v["pending"])
        paths = diff(v["active"], v["pending"])
        if not paths:
            errors.append("the pending version does not change the active policy")
        scope = classify(paths)
        return True, {"ok": not errors, "errors": errors, "version": v["version"], "from_version": v["from_version"], "changed_paths": paths,
                      "has_unimplemented": bool(scope["unimplemented"]), **scope}
