"""Poll readiness until ready, a checkpoint, or the deadline (docs/07: checkpoints from the captured policy).

Emits operation.waiting at each policy checkpoint (status waiting), admits early on the first ready answer that
carries readiness + image digest + identity, and renews the lease from a helper thread (lease_keeper). Result:
ready | deadline | unknown (the provider never answered, so non-readiness is unproven) | lease_lost.

With a real adapter the source of truth for "the right release is running" is the deployment inputs' registry.application_image_digest
(read from the controller's inputs file), never an ops.instances row: a ready instance whose reported version is not that digest's
12-hex prefix is not ready (code digest_mismatch, reported when the wait ends at the deadline). Unreadable inputs fail the action.
"""
import json
import re
import time
from datetime import datetime, timezone

import providers
from cdr_common import advance, guarded, policy_section
from cdr_db import call
from lease_keeper import LeaseKeeper
from st2common.runners.base_action import Action


def _ts(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _now():
    return datetime.now(timezone.utc)


def expected_digest(config):
    """registry.application_image_digest from the inputs file on the controller, or None when it cannot be read as a sha256 digest."""
    path = ((config or {}).get("ansible") or {}).get("inputs_path")
    try:
        with open(path) as f:
            digest = json.load(f)["registry"]["application_image_digest"]
    except (TypeError, OSError, ValueError, KeyError):
        return None
    return digest if isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) else None


class WaitReady(Action):
    @guarded
    def run(self, operation_id, lease_generation, provider, resource_id, deadline_at, checkpoints_at, poll_seconds=5):
        adapter = providers.load(self.config)
        ok, coordination = policy_section(self.config, operation_id, "coordination")
        if not ok:
            return False, coordination
        deadline = _ts(deadline_at)
        checkpoints = [_ts(c) for c in checkpoints_at]
        emitted = 0
        last = "not_ready"
        expected = None
        if not adapter.simulated:
            expected = expected_digest(self.config)
            if expected is None:
                return False, "inputs_unavailable"
        mismatch = False
        with LeaseKeeper(self.config, operation_id, lease_generation, coordination["lease_renewal_seconds"]) as keeper:
            while True:
                keeper.check()
                if adapter.simulated:
                    check = adapter.check_ready(provider, resource_id)
                else:
                    # Real adapter: /health/ready is probed at the instance address. The readiness body carries only a short version (the first 12 hex
                    # characters of the image digest), which must match the digest in the deployment inputs; that digest is what ready reports.
                    ok, rows = call(self.config, "SELECT public_ip::text FROM ops.instances WHERE provider = %s AND provider_resource_id = %s"
                                    " AND lifecycle_state <> 'deleted' ORDER BY created_at DESC LIMIT 1", (provider, resource_id))
                    if not ok:
                        return False, rows
                    ip = (rows[0][0] or "").split("/")[0] if rows else ""
                    check = adapter.check_ready(provider, resource_id, ip=ip or None)
                    mismatch = False
                    if check["state"] == "ready":
                        version = str(check.get("version") or "")
                        if len(version) >= 12 and expected.startswith("sha256:" + version):
                            check = {**check, "image_digest": expected}
                        else:
                            mismatch = True
                            check = {**check, "state": "not_ready", "image_digest": None}
                last = check["state"]
                if last == "ready" and check.get("image_digest") and check.get("identity"):
                    return True, {"result": "ready", "image_digest": check["image_digest"], "identity": check["identity"], "checkpoints_emitted": emitted}
                while emitted < len(checkpoints) and _now() >= checkpoints[emitted]:
                    emitted += 1
                    ok, out = advance(self.config, operation_id, "operation.waiting", "wait_ready", "waiting", f"readiness checkpoint {emitted}/{len(checkpoints)}: not ready yet")
                    if not ok:
                        return False, out
                if _now() >= deadline:
                    out = {"result": "unknown" if last == "unknown" else "deadline", "checkpoints_emitted": emitted}
                    if mismatch:
                        out["code"] = "digest_mismatch"
                    return True, out
                time.sleep(poll_seconds)
