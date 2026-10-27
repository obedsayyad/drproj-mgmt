"""apply_policy verify (docs/03 activation 5, phase verifying): what the running Prometheus loaded, not what was written."""
from cdr_common import guarded, policy_versions
from cdr_db import call
from prom_target import load
from st2common.runners.base_action import Action


class PolicyVerify(Action):
    @guarded
    def run(self, operation_id):
        ok, v = policy_versions(self.config, operation_id)
        if not ok:
            return False, v
        ok, out = call(self.config, "SELECT ops.set_policy_phase(%s::uuid, %s, 'verifying')", (operation_id, v["version"]))
        if not ok:
            return False, out
        loaded = load(self.config).loaded_version()
        if loaded != str(v["version"]):
            return False, "verify_failed"
        return True, {"loaded_label": loaded}
