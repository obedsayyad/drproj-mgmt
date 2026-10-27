# Management VM host notes (NOT applied by P3.1)

Nothing in this repo touches a host in P3. These are the host-level settings the management VM needs;
Ansible applies them in P5 and `scripts/check-perms.sh` verifies the permission ones.

- **Swap:** a 2 GB swapfile (`/swapfile`, mode 0600, `mkswap`/`swapon`, persisted in `/etc/fstab`) and
  `vm.swappiness=10` in `/etc/sysctl.d/`. The VM is 2 vCPU / 4 GB; the always-on Compose `mem_limit` ceilings sum to 3568 MB
  (see `compose.yaml`) and must be measured with a full stack plus a Terraform/restore job (T18).
- **Directories (all owned by the deploy user):** `/etc/drproj` 0700 holding each role's `.env` (0600) and
  `secrets/` (0700, files 0600); `/etc/drproj/ca` 0700 with `ca.key` 0400; the `pg_dump` backup directory 0700
  (files 0600); the Terraform-state directory 0700 (per provider/slot subdirectories, used from P5/P7).
- **Check:** `BACKUP_DIR=... TFSTATE_DIR=... scripts/check-perms.sh` (exit 1 on any deviation).
- **Heavy jobs:** one at a time (`resource_class=heavy`, concurrency 1, set in StackStorm policy in P4). The
  restore-test Postgres is a Compose profile (`restore-test`) and must not run beside capacity operations.
- **Provider tokens:** scoped, expiring tokens are a P5 input; P3 only reserves the `.env` names.
