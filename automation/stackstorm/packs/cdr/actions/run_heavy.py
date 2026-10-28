"""The single heavy-class wrapper (plan Q7): StackStorm has no resource_class, so heavy work runs through this action and
policies/heavy.yaml limits it to one at a time. It is the ONLY path to Terraform (docs/07): terraform_apply creates or deletes one
slot's VM through scripts/tf-slot.sh, terraform_output reads its outputs. assert_lease runs immediately before the adapter call and a
LeaseKeeper renews the lease every lease_renewal_seconds (20 s) for as long as the call runs.

deploy / ansible_deploy: with the simulation adapter both run the simulated deploy; with the real adapter both run `ansible-playbook app.yml`
limited to the instance, looked up in ops.instances by (provider, provider_resource_id). A failed or timed-out run is a failed attempt, never
ambiguous: the VM exists. The action timeout (run_heavy.yaml) must be at least the readiness deadline;
the Terraform subprocess gets ACTION_TIMEOUT_S minus a margin so a timed-out apply is reported here instead of by a killed action.
"""
import cdr_db
import providers
import restore_probe
from cdr_common import guarded, policy_section
from lease_keeper import LeaseKeeper, fenced
from st2common.runners.base_action import Action

ACTION_TIMEOUT_S = 900
TF_MARGIN_S = 60
TASKS = ("deploy", "terraform_apply", "terraform_output", "ansible_deploy", "restore_probe")
DEPLOY_TASKS = ("deploy", "ansible_deploy")
TARGET_SQL = "SELECT i.id::text, i.public_ip::text FROM ops.instances i WHERE i.provider = %s AND i.provider_resource_id = %s AND NOT i.simulated"


class RunHeavy(Action):
    @guarded
    def run(self, task, resource_class, operation_id, lease_generation, provider=None, resource_id=None, slot_key=None, attempt=1, exists=None):
        if task not in TASKS:
            return False, "bad_argument"
        if task in DEPLOY_TASKS and not resource_id:
            return False, "bad_argument"
        if task != "restore_probe" and provider not in ("digitalocean", "linode"):
            return False, "bad_argument"
        if task.startswith("terraform") and not slot_key:
            return False, "bad_argument"
        if task == "terraform_apply" and not isinstance(exists, bool):
            return False, "bad_argument"
        if task == "restore_probe":
            return self._restore_probe(resource_class, operation_id, lease_generation)
        adapter = providers.load(self.config)
        ok, coordination = policy_section(self.config, operation_id, "coordination")
        if not ok:
            return False, coordination
        ok, capacity = policy_section(self.config, operation_id, "capacity")
        if not ok:
            return False, capacity
        spec = {"warm_provider": capacity.get("warm_provider"), "timeout": ACTION_TIMEOUT_S - TF_MARGIN_S}
        target = None
        if task in DEPLOY_TASKS and not adapter.simulated:
            ok, rows = cdr_db.call(self.config, TARGET_SQL, (provider, resource_id))
            if not ok:
                return False, rows
            if len(rows) != 1 or not rows[0][1]:
                return True, {"result": "failed", "code": "instance_not_found", "task": task, "resource_class": resource_class, "simulated": False}
            target = {"instance_id": rows[0][0], "public_ip": rows[0][1].split("/")[0]}
        with LeaseKeeper(self.config, operation_id, lease_generation, coordination["lease_renewal_seconds"]) as keeper:
            if task in DEPLOY_TASKS and adapter.simulated:
                res = fenced(self.config, operation_id, lease_generation, lambda: adapter.deploy(provider, resource_id), keeper)
            elif task in DEPLOY_TASKS:
                res = fenced(self.config, operation_id, lease_generation, lambda: adapter.ansible_deploy(provider, resource_id, target, spec), keeper)
            elif task == "terraform_apply":
                res = fenced(self.config, operation_id, lease_generation,
                             lambda: adapter.tf_apply(provider, slot_key, operation_id, attempt, exists, spec), keeper)
            else:
                res = fenced(self.config, operation_id, lease_generation, lambda: adapter.tf_output(provider, slot_key, spec), keeper)
        return True, {**res, "task": task, "resource_class": resource_class, "simulated": adapter.simulated}

    def _restore_probe(self, resource_class, operation_id, lease_generation):
        """T18 heavy half: dump the app schema, restore it into a scratch database on restore-test-pg, compare, clean up. No provider is involved."""
        source, target = self.config.get("backup_db_url"), self.config.get("restore_test_url")
        if not source or not target:
            return False, "restore_probe_config_missing"
        ok, coordination = policy_section(self.config, operation_id, "coordination")
        if not ok:
            return False, coordination
        with LeaseKeeper(self.config, operation_id, lease_generation, coordination["lease_renewal_seconds"]) as keeper:
            res = fenced(self.config, operation_id, lease_generation,
                         lambda: restore_probe.probe(source, target, operation_id, self.config.get("pg_dump_bin") or "pg_dump",
                                                     self.config.get("pg_restore_bin") or "pg_restore", ACTION_TIMEOUT_S - TF_MARGIN_S), keeper)
        return True, {**res, "task": "restore_probe", "resource_class": resource_class, "simulated": False}
