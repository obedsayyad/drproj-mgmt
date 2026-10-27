"""Runs `ansible-playbook app.yml` against ONE instance for the real adapter (docs/07). Only called from cdr.run_heavy.

The environment handed to ansible-playbook is built here, not inherited: PATH/HOME, the dynamic-inventory database URL and the names of
the secrets the playbook reads (env:// references from the deployment inputs). Nothing secret is in argv; Ansible output is parsed for the
per-host stats the app role reports and is never returned or logged. A failed or timed-out run is a FAILED attempt, never ambiguous: the VM
already exists, so there is nothing to reconcile before a retry.
"""
import json
import os
import shutil
import subprocess

from .base import AdapterConfigError
from .tf_runner import required

PASS_ENV = ("PATH", "HOME", "LANG", "ANSIBLE_COLLECTIONS_PATH")
ALWAYS_PASS = ("NODE_EXPORTER_BASIC_AUTH_PASSWORD",)
REF_KEYS = (("supabase", "app_connection_reference"), ("registry", "pull_credential_reference"), ("tls", "backend_trust_bundle_reference"))


def ref_name(ref):
    return ref[len("env://"):] if isinstance(ref, str) and ref.startswith("env://") else ref


class AnsibleRunner:
    def __init__(self, cfg, ops_url=None):
        self.playbook_dir = required(cfg, "playbook_dir")
        self.inputs_path = required(cfg, "inputs_path")
        self.ssh_key_path = required(cfg, "ssh_key_path")
        self.ansible_bin = cfg.get("ansible_bin") or "ansible-playbook"
        self.ops_url = ops_url

    def _secret_names(self):
        try:
            with open(self.inputs_path) as f:
                inputs = json.load(f)
        except (OSError, ValueError) as exc:
            raise AdapterConfigError("deployment_inputs_unreadable") from exc
        names = list(ALWAYS_PASS)
        for section, key in REF_KEYS:
            name = ref_name((inputs.get(section) or {}).get(key))
            if name:
                names.append(name)
        return names

    def _env(self):
        env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
        env.update({k: os.environ[k] for k in self._secret_names() if k in os.environ})
        found = shutil.which(self.ansible_bin, path=env.get("PATH"))
        if found:  # the inventory script runs with the Python that has psycopg: the Ansible venv comes first on PATH
            env["PATH"] = os.path.dirname(os.path.realpath(found)) + os.pathsep + env.get("PATH", "")
        env["ANSIBLE_CONFIG"] = os.path.join(self.playbook_dir, "ansible.cfg")
        # the json callback lives in ansible.posix since ansible-core 2.13; custom stats (set_stats) are off unless asked for
        env["ANSIBLE_STDOUT_CALLBACK"] = "ansible.posix.json"
        env["ANSIBLE_SHOW_CUSTOM_STATS"] = "1"
        env["ANSIBLE_NOCOLOR"] = "1"
        env["CLOUD_VM_SSH_KEY_FILE"] = self.ssh_key_path
        if self.ops_url:
            env["DR_AUTOMATION_URL"] = self.ops_url
        return env

    def run(self, instance_id, timeout=840):
        """-> (rc, stats). rc None means the call timed out (the process was killed); stats is the app role's per-host set_stats or {}."""
        env = self._env()
        argv = [self.ansible_bin, "-i", os.path.join(self.playbook_dir, "inventory", "cloud_app.py"), "-e", f"inputs_file={self.inputs_path}",
                "--limit", str(instance_id), os.path.join(self.playbook_dir, "playbooks", "app.yml")]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, check=False, cwd=self.playbook_dir)
        except subprocess.TimeoutExpired:
            return None, {}
        except OSError:
            return 127, {}
        return p.returncode, parse_stats(p.stdout, instance_id)


def parse_stats(stdout, instance_id):
    """Pull the {ready, instance_id, digest} facts for one host out of the json callback output."""
    try:
        doc = json.loads(stdout[stdout.index("{"):])
    except (ValueError, TypeError):
        return {}
    custom = doc.get("custom_stats") or {}
    stats = custom.get(str(instance_id)) or {}
    return stats if isinstance(stats, dict) else {}
