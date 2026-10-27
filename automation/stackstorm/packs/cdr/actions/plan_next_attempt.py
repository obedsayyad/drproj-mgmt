"""After a cleaned (confirmed absent) attempt: retry the same provider while it has retries left, then the other
provider, then exhausted (docs/07). Every new attempt reserves a fresh slot through reserve_slot (cap enforced in SQL).
`attempts` maps provider -> attempts made so far. A warm or interim_warm slot_kind (standby repair) retries only on the SAME provider: the
standby never alternates (the warm slot belongs to the warm provider, an interim slot to the other one), so exhaustion ends in an incident."""
import providers
from cdr_common import guarded, policy_section
from cdr_db import call
from st2common.runners.base_action import Action

OTHER = {"digitalocean": "linode", "linode": "digitalocean"}


class PlanNextAttempt(Action):
    @guarded
    def run(self, operation_id, lease_generation, provider, attempts, slot_kind="elastic"):
        adapter = providers.load(self.config)
        ok, pol = policy_section(self.config, operation_id, "provisioning")
        if not ok:
            return False, pol
        per_provider = 1 + int(pol["retries_per_provider"])
        for candidate in ((provider,) if slot_kind != "elastic" else (provider, OTHER[provider])):
            if attempts.get(candidate, 0) >= per_provider or adapter.observe(candidate) != "available":
                continue
            ok, rows = call(
                self.config,
                "SELECT reserved_key, reserved_generation FROM ops.reserve_slot(%s::uuid, %s, %s, %s, %s)",
                (operation_id, lease_generation, candidate, slot_kind, adapter.simulated),
            )
            if not ok:
                return False, rows
            if rows:
                return True, {"plan": "retry_same" if candidate == provider else "alternate", "provider": candidate,
                              "slot_key": rows[0][0], "slot_generation": rows[0][1], "attempt": sum(attempts.values()) + 1}
        return True, {"plan": "exhausted", "attempts": sum(attempts.values())}
