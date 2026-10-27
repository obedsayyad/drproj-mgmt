"""Delete resources of one scope and confirm absence through the provider API (docs/07), shared by provider_cleanup
(an attempt's tags) and delete_slot_resource (a slot's tags).

Only project-tagged resources inside the given tag scope are touched; the tag listing is the scope, never a bare id.
`only_ids` (optional) makes the scope strict: a tagged resource outside it is not deleted and the result is `unknown`.
assert_lease runs immediately before every delete. result=cleaned carries evidence for release_slot; anything else
means absence is NOT proven.
"""
import time

from lease_keeper import fenced
from providers.base import PROJECT_TAG, now_iso

CONFIRM_TIMEOUT_S = 60


def absence_evidence(adapter, provider, slot_key, resource_ids, by=None):
    """-> (evidence, None) or (None, reason). Evidence for release_slot / resolve_held_slot that the slot holds no VM.

    Simulation: the caller's own provider answers are the evidence (unchanged). Real: ALL of (1) the slot's Terraform state shows zero
    resources, (2) provider get is `absent` for every known id, (3) list_by_tag(project, slot) is empty. Any other answer, including an
    unreadable state file or a provider that does not answer, is NOT absence: the caller keeps the slot held_uncertain.
    """
    ids = sorted(set(resource_ids))
    evidence = {"confirmed_absent": True, "source": "provider_api", "observed_at": now_iso(), "simulated": adapter.simulated, "resource_ids": ids}
    if by:
        evidence["by"] = by
    if adapter.simulated:
        return evidence, None
    count = adapter.slot_state_count(provider, slot_key)
    if count is None:
        return None, "slot_state_unreadable"
    if count:
        return None, "slot_state_not_empty"
    for rid in ids:
        state = adapter.get(provider, rid)["state"]
        if state != "absent":
            return None, "get_" + state
    listing = adapter.list_by_tag(provider, {**PROJECT_TAG, "slot": slot_key})
    if listing["state"] != "ok":
        return None, "list_unknown"
    if listing["resources"]:
        return None, "list_not_empty"
    evidence["checks"] = {"slot_state_resources": 0, "provider_get": "absent" if ids else "not_applicable", "list_by_tag": 0}
    return evidence, None


def delete_and_confirm(config, adapter, operation_id, lease_generation, provider, tags, keeper, resource_id=None,
                       confirm_poll_seconds=2, only_ids=None):
    listing = adapter.list_by_tag(provider, tags)
    ids = {r["resource_id"] for r in listing["resources"]} if listing["state"] == "ok" else set()
    if only_ids is not None and ids - set(only_ids):
        return {"result": "unknown", "reason": "unexpected_tagged_resource"}
    if resource_id:
        ids.add(resource_id)
    if listing["state"] != "ok" and not ids:
        return {"result": "unknown", "reason": "list_unknown"}
    for rid in sorted(ids):
        res = fenced(config, operation_id, lease_generation, lambda rid=rid: adapter.delete(provider, rid), keeper)
        if res["result"] != "ok":
            return {"result": "unknown", "reason": "delete_" + res["result"]}
    end = time.monotonic() + CONFIRM_TIMEOUT_S
    pending = set(ids)
    while pending:
        keeper.check()
        for rid in sorted(pending):
            state = adapter.get(provider, rid)["state"]
            if state == "unknown":
                return {"result": "unknown", "reason": "get_unknown"}
            if state == "absent":
                pending.discard(rid)
        if pending:
            if time.monotonic() >= end:
                return {"result": "still_present", "resource_ids": sorted(pending)}
            time.sleep(confirm_poll_seconds)
    after = adapter.list_by_tag(provider, tags)
    if after["state"] != "ok" or after["resources"]:
        return {"result": "unknown", "reason": "list_after"}
    evidence, why = absence_evidence(adapter, provider, tags["slot"], ids)
    if evidence is None:
        return {"result": "unknown", "reason": "absence_unproven_" + why}
    return {"result": "cleaned", "evidence": evidence}
