"""Provider adapter interface (docs/06 provider operations; plan Q8).

Result codes are explicit so a caller never treats "no answer" as "absent":
  create/delete/deploy -> {"result": "ok" | "timeout_ambiguous" | "unknown" | "unavailable", ...}
  ansible_deploy       -> {"result": "ok" | "failed", ...}; failed is final for the attempt (the VM exists, nothing to reconcile)
  get                  -> {"state": "present" | "absent" | "unknown", "power", "ip", "observed_at"}
  list_by_tag          -> {"state": "ok" | "unknown", "resources": [{resource_id, tags, ...}]}
  observe              -> "available" | "unavailable" | "unknown"
Real mapping: get 404 -> absent, get timeout/5xx -> unknown; create after Terraform apply started and failed -> timeout_ambiguous (never absent);
429/401/403 -> unavailable. See providers/real.py.
`simulated` is True only for the local simulation; it flows into reserve_slot and the operation's adapter_mode.
"""
from datetime import datetime, timezone

PROJECT_TAG = {"project": "cdr"}
# The image digest every deploy converges on. The simulation reports it; the real adapter reads it from the deployed host (P5.3).
PINNED_DIGEST = "sha256:" + "0" * 64


class AdapterConfigError(Exception):
    """provider_adapter missing or unknown. There is deliberately no default."""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tags_for(slot_key, operation_id, attempt):
    """Tags every created resource carries; only project-tagged resources are eligible for cleanup."""
    return {**PROJECT_TAG, "slot": slot_key, "op": str(operation_id), "attempt": str(attempt)}


class ProviderAdapter:
    simulated = False
    mode = "real"

    def create(self, provider, slot_key, operation_id, attempt, tags, spec):
        raise NotImplementedError

    def get(self, provider, resource_id):
        raise NotImplementedError

    def delete(self, provider, resource_id):
        raise NotImplementedError

    def list_by_tag(self, provider, tags):
        raise NotImplementedError

    def observe(self, provider):
        raise NotImplementedError

    def deploy(self, provider, resource_id):
        raise NotImplementedError

    def ansible_deploy(self, provider, resource_id, target, spec=None):
        """Deploy to one existing instance. -> {"result": "ok", "state", "identity", "image_digest"} | {"result": "failed", "code"}"""
        raise NotImplementedError

    def check_ready(self, provider, resource_id):
        """-> {"state": "ready" | "not_ready" | "unknown", "image_digest", "identity"}"""
        raise NotImplementedError

    def tf_apply(self, provider, slot_key, operation_id, attempt, exists, spec=None):
        """Create (exists=True) or delete (exists=False) one slot's VM through Terraform. -> same result codes as create."""
        raise NotImplementedError

    def tf_output(self, provider, slot_key, spec=None):
        """-> {"result": "ok", "resource_id", "ip"} | {"result": "unknown"}"""
        raise NotImplementedError

    def set_routing(self, target, mode):
        """Routing is simulated until the HAProxy runtime API lands (P6)."""
        raise NotImplementedError

    def power_on(self, provider, resource_id):
        """-> {"result": "ok" | "unknown" | "unavailable"}"""
        raise NotImplementedError

    def pinned_digest(self):
        """The digest a ready instance must report to count as the deployed release."""
        raise NotImplementedError

    def drain(self, provider, resource_id):
        """-> "drained" | "busy" | "unknown" """
        raise NotImplementedError
