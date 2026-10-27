# Ansible (P5.3)

Configures the four pre-created self-hosted hosts and deploys the relief app to application hosts. Run from the developer workstation
(bootstrap) or from the StackStorm runner (`ansible_deploy` through `cdr.run_heavy`). See `docs/docs/07-automation.md`, `docs/docs/11-build-and-demo-plan.md`
(Bootstrap) and the "First bootstrap" runbook in `docs/docs/15-runbooks.md`.

- The fixed hosts are operated as root (approved deviation, `inventory/group_vars/fixed.yml`); bootstrap runs from the management VM (`/opt/drproj/ansible`, venv `/opt/drproj/ansible-venv`).
- `scripts/gen-inventory.py` writes `inventory/generated.yml` from `deploy/inputs/deployment-inputs.local.json`.
- `inventory/cloud_app.py` is the dynamic inventory of cloud VMs (`ops.instances`, `DR_AUTOMATION_URL`).
- Roles: `bootstrap`, `app`, `ca` (implemented); `lb` (P6), `mgmt` (P6/P7), `testing` (P8) are scaffolds that fail with `not_implemented`.
- Secrets are never in this tree: `env://NAME` references resolve from the controller environment; Ansible Vault is not used.
