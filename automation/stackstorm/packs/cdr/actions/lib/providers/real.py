"""Real provider adapter (docs/07, "Real adapter"). Terraform (through scripts/tf-slot.sh, only ever called from cdr.run_heavy)
creates and deletes; the DigitalOcean and Linode HTTP APIs answer get / list_by_tag / observe / power_on.

Result mapping (never "absent" from silence):
  tf_apply               active provider's token unset -> unavailable (terraform not run); the other provider's token is not needed
  tf_apply exists=true   observe unavailable -> unavailable (terraform not run); observe unknown -> unknown (not run);
                         script refused or terraform init failed (before "tf-slot: apply started") -> unknown;
                         non-zero exit or timeout after apply started, output failure, or get absent/unknown -> timeout_ambiguous;
                         otherwise ok with resource_id, ip, vcpu, memory_mb
  tf_apply exists=false  not started -> unknown; non-zero/timeout after start -> timeout_ambiguous; exit 0 -> ok
  delete                 ok only when no slot state of the provider still holds the id; otherwise unknown
ansible_deploy        ansible-playbook app.yml limited to one instance (AnsibleRunner): rc 0 with matching ready/identity/digest facts -> ok;
                      non-zero, timeout, or facts that do not verify -> failed (never timeout_ambiguous: the VM exists)
check_ready / set_routing / drain / pinned_digest stay unimplemented (HAProxy P6).
"""
import json

from .base import AdapterConfigError, ProviderAdapter, tags_for
from .ansible_runner import AnsibleRunner
from .http_api import DigitalOceanHttp, LinodeHttp
from .tf_runner import TfRunner, required

PROVIDERS = ("digitalocean", "linode")
DEFAULT_VCPU = 1
DEFAULT_MEMORY_MB = 1024


class RealAdapter(ProviderAdapter):
    simulated = False
    mode = "real"

    def __init__(self, cfg, http=None, runner=None, ansible=None, ops_url=None):
        self.cfg = cfg
        required(cfg, "tf_slot_script")
        required(cfg, "tf_state_dir")
        for p in PROVIDERS:
            for k in ("token_env", "region", "size", "image", "ssh_key_id_env"):
                required(cfg, p, k)
        self.http = http or {"digitalocean": DigitalOceanHttp(cfg["digitalocean"]["token_env"]), "linode": LinodeHttp(cfg["linode"]["token_env"])}
        self.runner = runner or TfRunner(cfg["tf_slot_script"], cfg["tf_state_dir"],
                                         {p: cfg[p]["token_env"] for p in PROVIDERS}, cfg.get("terraform_bin"))
        self.ansible = ansible or (AnsibleRunner(cfg["ansible"], ops_url) if cfg.get("ansible") else None)

    # ---- HTTP side
    def _api(self, provider):
        if provider not in self.http:
            raise AdapterConfigError("provider_unknown")
        return self.http[provider]

    def get(self, provider, resource_id):
        return self._api(provider).get(resource_id)

    def list_by_tag(self, provider, tags):
        return self._api(provider).list_by_tag(tags)

    def observe(self, provider):
        return self._api(provider).observe()

    def power_on(self, provider, resource_id):
        return self._api(provider).power_on(resource_id)

    # ---- Terraform side
    def _vars(self, provider, operation_id, attempt, exists):
        from .http_api import token_from_ref

        c = self.cfg[provider]
        return {"exists": "true" if exists else "false", "op": str(operation_id), "attempt": int(attempt), "region": c["region"], "size": c["size"],
                "image": c["image"], "ssh_key_id": token_from_ref(c["ssh_key_id_env"])}

    def tf_apply(self, provider, slot_key, operation_id, attempt, exists, spec=None):
        spec = spec or {}
        timeout = spec.get("timeout", 840)
        if not self.runner.has_token(provider):
            return {"result": "unavailable", "reason": "provider_token_missing"}
        if exists:
            seen = self.observe(provider)
            if seen != "available":
                return {"result": "unavailable" if seen == "unavailable" else "unknown", "reason": "preflight_" + seen}
        rc, started, _ = self.runner.run(provider, slot_key, "apply", self._vars(provider, operation_id, attempt, exists), spec.get("warm_provider"), timeout)
        if not started:
            return {"result": "unknown", "reason": "terraform_not_started"}
        if rc != 0:
            res = {"result": "timeout_ambiguous", "reason": "terraform_timeout" if rc is None else "terraform_failed"}
            if exists:
                rid = self._output(provider, slot_key, spec.get("warm_provider")).get("resource_id")
                res["resource_id"] = rid
            return res
        if not exists:
            return {"result": "ok"}
        out = self._output(provider, slot_key, spec.get("warm_provider"))
        rid = out.get("resource_id")
        if not rid:
            return {"result": "timeout_ambiguous", "reason": "terraform_output_missing", "resource_id": None}
        got = self.get(provider, rid)
        if got["state"] != "present":
            return {"result": "timeout_ambiguous", "reason": "get_" + got["state"], "resource_id": rid}
        return {"result": "ok", "resource_id": rid, "ip": out.get("ip") or got.get("ip"),
                "vcpu": got.get("vcpu") or DEFAULT_VCPU, "memory_mb": got.get("memory_mb") or DEFAULT_MEMORY_MB}

    def create(self, provider, slot_key, operation_id, attempt, tags, spec):
        return self.tf_apply(provider, slot_key, operation_id, attempt, True, spec)

    def _output(self, provider, slot_key, warm_provider):
        rc, _, stdout = self.runner.run(provider, slot_key, "output", None, warm_provider, 60)
        if rc != 0:
            return {}
        try:
            data = json.loads(stdout)
            rid = (data.get("resource_id") or {}).get("value")
            return {"resource_id": str(rid) if rid is not None else None, "ip": (data.get("public_ip") or {}).get("value")}
        except (ValueError, AttributeError):
            return {}

    def tf_output(self, provider, slot_key, spec=None):
        out = self._output(provider, slot_key, (spec or {}).get("warm_provider"))
        return {"result": "ok", **out} if out.get("resource_id") else {"result": "unknown", "reason": "terraform_output_missing"}

    def delete(self, provider, resource_id):
        """Deletion IS `tf_apply exists=false` (run_heavy). This only confirms Terraform no longer holds the id; it deletes nothing."""
        ids = self.runner.provider_resource_ids(provider)
        if ids is None:
            return {"result": "unknown", "reason": "terraform_state_unreadable"}
        return {"result": "unknown", "reason": "terraform_state_not_empty"} if str(resource_id) in ids else {"result": "ok"}

    def slot_state_count(self, provider, slot_key):
        """Resources recorded in the slot's Terraform state; None when unreadable."""
        ids = self.runner.slot_resource_ids(provider, slot_key)
        return None if ids is None else len(ids)

    # ---- Ansible side
    def ansible_deploy(self, provider, resource_id, target, spec=None):
        """Deploy the pinned relief image to one existing instance. target is the ops.instances row {"instance_id", "public_ip"}."""
        if self.ansible is None:
            raise AdapterConfigError("real_adapter_config_missing")
        rc, stats = self.ansible.run(target["instance_id"], (spec or {}).get("timeout", 840))
        if rc is None:
            return {"result": "failed", "code": "deploy_timeout"}
        if rc != 0:
            return {"result": "failed", "code": "deploy_failed"}
        if stats.get("ready") is not True or str(stats.get("instance_id")) != str(target["instance_id"]) or not stats.get("digest"):
            return {"result": "failed", "code": "deploy_unverified"}
        return {"result": "ok", "state": "ready", "identity": stats["instance_id"], "image_digest": stats["digest"]}
