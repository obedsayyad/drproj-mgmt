"""apply_policy compile + apply (docs/03 activation 3-5, phases compiling then applying). Re-renders external_labels.policy_version
of the live prometheus.yml to the pending version, then reloads Prometheus (`policy_compile`: simulation or real; no default).
Fails with a short code when the file cannot be rendered or the reload does not succeed; the previous label is returned so the
rollback task can put it back. lease_generation > 0 (a capacity change) is asserted right before the write."""
from cdr_common import guarded, policy_versions
from cdr_db import call
from lease_keeper import fenced
from prom_render import read_policy_version, set_policy_version
from prom_target import load
from st2common.runners.base_action import Action


class PolicyCompile(Action):
    @guarded
    def run(self, operation_id, lease_generation=0):
        ok, v = policy_versions(self.config, operation_id)
        if not ok:
            return False, v
        target = load(self.config)
        ok, out = call(self.config, "SELECT ops.set_policy_phase(%s::uuid, %s, 'compiling')", (operation_id, v["version"]))
        if not ok:
            return False, out
        try:
            current = target.read()
            rendered = set_policy_version(current, v["version"])
        except (OSError, ValueError) as exc:
            return False, "template_missing" if isinstance(exc, OSError) else str(exc)
        ok, out = call(self.config, "SELECT ops.set_policy_phase(%s::uuid, %s, 'applying')", (operation_id, v["version"]))
        if not ok:
            return False, out
        previous = read_policy_version(current)

        def apply():
            target.write(rendered)
            return target.reload()

        reloaded = fenced(self.config, operation_id, lease_generation, apply) if lease_generation else apply()
        if not reloaded:
            return False, "reload_failed"
        return True, {"label": str(v["version"]), "previous_label": previous, "mode": target.mode}
