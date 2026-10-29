"""Runs scripts/tf-slot.sh for the real adapter (docs/07). Only called from cdr.run_heavy.

The environment handed to the script is built here, not inherited: PATH/HOME, the state dir, the warm provider and ONLY the active
provider's token (each provider has its own Terraform root; the other provider's token is never exported and may be unset). Tokens are
never in argv. Terraform's output is read only for the apply-started marker and,
for `output`, as JSON; it is never returned or logged.
"""
import json
import os
import subprocess

from .base import AdapterConfigError
from .http_api import token_from_ref

APPLY_STARTED = "tf-slot: apply started"
PASS_ENV = ("PATH", "HOME", "TERRAFORM", "TF_CLI_CONFIG_FILE", "TF_IN_AUTOMATION", "TF_PLUGIN_CACHE_DIR")


TOKEN_ENV = {"digitalocean": "DIGITALOCEAN_TOKEN", "linode": "LINODE_TOKEN"}


class TfRunner:
    def __init__(self, script, state_dir, token_refs, terraform_bin=None):
        self.script = script
        self.state_dir = state_dir
        self.token_refs = token_refs  # {"digitalocean": ref, "linode": ref}
        self.terraform_bin = terraform_bin

    def has_token(self, provider):
        try:
            token_from_ref(self.token_refs[provider])
        except (AdapterConfigError, KeyError):
            return False
        return True

    def _env(self, provider, slot, warm_provider=None):
        """Environment for one slot's Terraform run. Raises AdapterConfigError("real_token_missing") when the active provider's token is unset."""
        if provider not in TOKEN_ENV:
            raise AdapterConfigError("provider_unknown")
        env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
        env["TF_STATE_DIR"] = self.state_dir
        if self.terraform_bin:
            env["TERRAFORM"] = self.terraform_bin
        if warm_provider:
            env["WARM_PROVIDER"] = warm_provider
        env[TOKEN_ENV[provider]] = token_from_ref(self.token_refs[provider])
        return env

    def run(self, provider, slot, action, variables=None, warm_provider=None, timeout=840):
        """-> (rc, apply_started, stdout). rc None means the call timed out (the process was killed)."""
        env = self._env(provider, slot, warm_provider)  # before anything runs: a missing active-provider token starts no Terraform
        argv = [self.script, provider, slot, action]
        for k, v in (variables or {}).items():
            argv += ["-var", f"{k}={v}"]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, check=False)
            return p.returncode, APPLY_STARTED in (p.stderr or ""), p.stdout
        except subprocess.TimeoutExpired as exc:
            err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return None, APPLY_STARTED in err, ""
        except OSError:
            return 127, False, ""
        finally:
            self.tighten(provider, slot)

    def tighten(self, provider, slot):
        """Terraform creates state files executable (0777 & ~umask = 0700); docs/09 wants state files 0600 and directories 0700."""
        base = os.path.join(self.state_dir, provider, slot)
        for root, dirs, files in os.walk(base):
            try:
                for d in dirs:
                    os.chmod(os.path.join(root, d), 0o700)
                for f in files:
                    os.chmod(os.path.join(root, f), 0o600)
            except OSError:
                return  # best effort: a permissions problem must not turn a finished apply into a different failure

    # ---- state files (read directly; terraform is not run)
    def _state_ids(self, path):
        try:
            with open(path) as f:
                state = json.load(f)
        except FileNotFoundError:
            return set()
        except (OSError, ValueError):
            return None
        ids = set()
        for res in state.get("resources", []):
            if res.get("mode") != "managed":
                continue
            for inst in res.get("instances", []):
                ids.add(str((inst.get("attributes") or {}).get("id")))
        return ids

    def slot_resource_ids(self, provider, slot):
        """Resource ids recorded in one slot's state; empty set = no resources; None = the state cannot be read (not evidence of absence)."""
        return self._state_ids(os.path.join(self.state_dir, provider, slot, "terraform.tfstate"))

    def provider_resource_ids(self, provider):
        base = os.path.join(self.state_dir, provider)
        try:
            slots = sorted(os.listdir(base))
        except FileNotFoundError:
            return set()
        except OSError:
            return None
        out = set()
        for s in slots:
            ids = self._state_ids(os.path.join(base, s, "terraform.tfstate"))
            if ids is None:
                return None
            out |= ids
        return out


def required(cfg, *path):
    cur = cfg
    for k in path:
        if not isinstance(cur, dict) or not cur.get(k):
            raise AdapterConfigError("real_adapter_config_missing")
        cur = cur[k]
    return cur
