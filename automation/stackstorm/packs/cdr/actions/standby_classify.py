"""Classify the warm slot for standby repair (docs/04 Warm standby repair step 3). Read-only; the answer comes from the slot table, the
provider API (get + tag listing) and the readiness check, never from a timeout.

  healthy         present, powered on, ready with the pinned digest (the record was stale: only the state is written back)
  app_broken      present, powered on, not ready                       -> redeploy
  powered_off     present, powered off                                 -> power on, then redeploy
  absent          slot free, or the provider API listed and got "absent" and no other tagged resource claims the slot (evidence returned)
  held_uncertain  the slot is already held_uncertain (an interim warm may be needed; `interim_bound` says whether one exists)
  unknown         the provider did not answer, or tags show a resource nothing explains: hold the slot, never create
  busy            reserved or releasing under another operation: reconcile's, not ours
  maintenance     maintenance mode is on: nothing is repaired now
`other_provider` / `other_available` tell the interim-warm branch where it may go (the provider that is not the warm slot's).
"""
import providers
from cdr_common import guarded, policy_section
from cdr_db import call
from cleanup import absence_evidence
from providers.base import PROJECT_TAG, now_iso
from st2common.runners.base_action import Action


class StandbyClassify(Action):
    @guarded
    def run(self, operation_id, lease_generation):
        adapter = providers.load(self.config)
        ok, rows = call(
            self.config,
            "SELECT s.key, s.generation, s.provider, s.state, i.provider_resource_id FROM ops.capacity_slots s"
            " LEFT JOIN ops.instances i ON i.id = s.bound_instance_id WHERE s.key = 'warm'",
            (),
        )
        if not ok:
            return False, rows
        key, generation, provider, state, rid = rows[0]
        ok, cap = policy_section(self.config, operation_id, "capacity")
        if not ok:
            return False, cap
        ok, interim = call(self.config, "SELECT count(*) FROM ops.capacity_slots WHERE key LIKE 'interim-warm-%%' AND state = 'bound'", ())
        if not ok:
            return False, interim
        ok, rows = call(self.config, "SELECT maintenance FROM ops.runtime_flags WHERE singleton", ())
        if not ok:
            return False, rows
        maintenance = not rows or bool(rows[0][0])  # no flag row: fail closed
        warm_provider = cap["warm_provider"] if state == "free" else provider
        other = {"digitalocean": "linode", "linode": "digitalocean"}[warm_provider]
        base = {"slot_key": key, "slot_generation": generation, "slot_state": state, "provider": provider, "resource_id": rid or "",
                "interim_bound": bool(interim[0][0]), "simulated": adapter.simulated,
                "other_provider": other, "other_available": adapter.observe(other) == "available"}
        if maintenance:
            return True, {**base, "class": "maintenance"}
        if state == "held_uncertain":
            return True, {**base, "class": "held_uncertain"}
        if state in ("reserved", "releasing"):
            return True, {**base, "class": "busy"}
        if state == "free":
            provider = cap["warm_provider"]  # a free warm slot follows the configured warm provider
            if adapter.observe(provider) != "available":
                return True, {**base, "provider": provider, "class": "unknown", "reason": "provider_" + adapter.observe(provider)}
            return True, {**base, "provider": provider, "class": "absent", "evidence": None}
        # bound
        if not rid:
            return True, {**base, "class": "unknown", "reason": "no_resource_id"}
        got = adapter.get(provider, rid)
        listing = adapter.list_by_tag(provider, {**PROJECT_TAG, "slot": key})
        if got["state"] == "unknown" or listing["state"] != "ok":
            return True, {**base, "class": "unknown", "reason": "provider_answer_unknown"}
        others = sorted({r["resource_id"] for r in listing["resources"]} - {rid})
        if others:
            return True, {**base, "class": "unknown", "reason": "unexpected_tagged_resource"}  # never delete or adopt by tag alone
        if got["state"] == "absent":
            evidence, why = absence_evidence(adapter, provider, key, [rid], by="standby_repair")
            if evidence is None:
                return True, {**base, "class": "unknown", "reason": "absence_unproven_" + why}
            return True, {**base, "class": "absent", "evidence": evidence}
        if got.get("power") == "off":
            return True, {**base, "class": "powered_off"}
        if adapter.simulated:
            check = adapter.check_ready(provider, rid)
            deployed, pinned = check.get("image_digest"), adapter.pinned_digest()
        else:
            # Real adapter: /health/ready needs the instance address, and the digests are the ones the deploys recorded (as standby_verify does):
            # the instance's own recorded digest must equal the primary's. A missing record reads as "not deployed yet", i.e. app_broken -> redeploy.
            check = adapter.check_ready(provider, rid, ip=got.get("ip"))
            ok, mine = call(self.config, "SELECT i.image_digest FROM ops.capacity_slots s JOIN ops.instances i ON i.id = s.bound_instance_id WHERE s.key = %s", (key,))
            if not ok:
                return False, mine
            ok, prim = call(self.config, "SELECT image_digest FROM ops.instances WHERE role = 'primary' AND image_digest IS NOT NULL ORDER BY last_seen_at DESC NULLS LAST LIMIT 1", ())
            if not ok:
                return False, prim
            deployed, pinned = (mine[0][0] if mine else None), (prim[0][0] if prim else None)
        if check["state"] == "unknown":
            return True, {**base, "class": "unknown", "reason": "readiness_unknown"}
        if check["state"] == "ready" and pinned is not None and deployed == pinned:
            return True, {**base, "class": "healthy", "image_digest": deployed}
        return True, {**base, "class": "app_broken"}
