# CLOUD-1 evidence: real adapter enabled, first real warm standby (DigitalOcean)

Run 2026-10-29 ~11:25-14:35Z from the management VM (`103.141.79.170`) against DigitalOcean and Linode. This was the first run with real provider tokens. Nothing secret is recorded here: tokens live only in `/etc/drproj/secrets/providers.env` (0600, management) and the runner container environment.

**Status: DONE, T28 partial.** The DigitalOcean warm standby is created, deployed, ready and UP as `warm_bk`. The task was meant to create nothing on Linode; the stack created Linode VMs by itself (below). The last one was adopted and released through the designed path (T29 / orphan adoption, section below), and Linode is back to 0 instances. T28 is partial: no single uninterrupted run from an empty warm slot to a verified backup was achieved, so the 8 minute RTO is not shown end to end.

## Final state (checked 14:30-14:35Z; Linode and incident rows rechecked 15:50Z after the adoption)

| Check | Result |
|---|---|
| `ops.instances` warm | digitalocean, droplet `613847824`, fra1, `s-1vcpu-1gb-intel`, `debian-13-x64`, `simulated=false`, lifecycle `ready`, routing `backup`, address `159.89.104.79` |
| `/api/overview` capacity | `warm_ready=1`, `occupied_digitalocean=1`, `occupied_linode=0`; all five integrations `fresh` |
| HAProxy (Runtime API `show servers state`) | `warm_bk` `159.89.104.79:8443` UP, admin state 0; `warm_act` maint; `iwarm_*` maint at 0.0.0.0 |
| `curl --cacert /etc/drproj/ca/ca-bundle.crt https://159.89.104.79:8443/health/ready` | 200, `instance_id` equals the recorded instance |
| Prometheus targets | `relief` and `node` up for the primary and for `159.89.104.79` (cloud targets `cloud-warm.json`) |
| Alerts firing | none (Prometheus `/api/v1/alerts` empty) |
| DigitalOcean droplets tagged `project:cdr` | exactly 1 (`613847824`, tags `slot:warm`, `op:3d2bf209…`, `attempt:1`) |
| Terraform state | `/var/lib/drproj/terraform/digitalocean/warm/` dir 0700, `terraform.tfstate` 0600, one `digitalocean_droplet` in state; nothing else under `/var/lib/drproj/terraform` (the empty `linode/interim-warm-linode` directory was removed after the release) |
| Linode instances | 0; slot `interim-warm-linode` free |
| Open incidents | none |

## Substitutions and deviations from the task inputs

- **Image:** `debian-13-x64` (DigitalOcean no longer offers `debian-12-x64`; `GET /v2/images/debian-12-x64` is 404) and `linode/debian13` (exists). The four fixed hosts stay on Debian 12 (bookworm); cloud VMs run Debian 13 (trixie). Docker 29.5.2 exists for both suites (`5:29.5.2-1~debian.12~bookworm`, `5:29.5.2-1~debian.13~trixie`). The bootstrap role takes the suite from `ansible_facts['distribution_release']` and accepts Debian 12 and 13.
- **Size:** `s-1vcpu-1gb-intel` instead of `s-1vcpu-1gb`. The preflight only checked that the slug exists, not that it is offered in the region; `s-1vcpu-1gb` is not available in fra1 (the `size.regions` list lacks it), so the first two Terraform applies failed and created nothing. fra1 offers 1 vCPU / 1 GB as `s-1vcpu-1gb-intel` ($7/mo against $6). docs/12 records it.
- **SSH key:** the `drproj` key did not exist at first; it was uploaded to both providers after the first preflight stopped the task. DigitalOcean key id is passed as `DIGITALOCEAN_SSH_KEY_ID`, the Linode label as `LINODE_SSH_KEY_LABEL` (both in `providers.env`).

## Timeline and per-step timings

Times are UTC from operation events and StackStorm executions. Operation `3d2bf209…` is the only run that created the droplet.

| Step | Time / elapsed |
|---|---|
| Trigger `standby_repair` (API, operator Basic Auth) | 12:21:54 accepted |
| Lease acquired | 12:22:04 (10 s) |
| Classified `absent` (provider evidence) | 12:22:13 (9 s) |
| Reserve warm slot, Terraform apply creates the droplet | event `create attempt 1` at 12:22:53 (about 40 s from classification) |
| Deploy (Ansible `app.yml`) | failed at 12:23:07: runner could not read the inputs file (see Root causes) |
| Later successful `ansible_deploy` through the workflow (`run_heavy`) | 35 s to 40 s (13:10:34-13:11:09, 13:20:37-13:21:17, 13:28:44-13:29:19) |
| First full `app.yml` by hand from the runner (bootstrap as root, Docker install, CA leaf signed on management, app up) | about 2 min, 40 ok / 0 failed |
| `wait_ready` with the fixed code | 5 s (13:29:20-13:29:25) |
| `routing warm -> backup`, `standby_verify` | 6 s and 4 s; HAProxy needs its own health checks (inter 10 s, rise 2) so verify now waits up to 45 s |
| Final healthy `standby_repair` `6e9de465…` (trigger to `operation.succeeded`) | 13:53:02 to 13:53:55 = 53 s |

Component times add up to well under the 8 minute RTO (create ~40 s + first deploy ~2 min + readiness ~5 s + routing/verify ~15 s), but they come from separate runs, so this is an estimate, not a measured end-to-end RTO.

## Linode episode (against the task constraint)

- 12:28-12:34, operation `627a3ae2…`: with the warm slot `held_uncertain` (the DO droplet existed but the deploy had failed), the workflow took its designed interim path: reserved `interim-warm-linode`, created a Linode VM (12:29:33), deploy failed twice, `provision_exhausted`, and the VM was removed by the workflow. Linode was back to 0 instances.
- 12:50-12:55, two alert-triggered `standby_repair` operations ran while I was recreating the runner container and rerunning `mgmt.yml`. One created Linode VM `109576462` at 12:51:28 and was killed mid-apply, so Terraform state for `interim-warm-linode` is empty while the VM exists. Reconcile reports it as `orphan:linode:109576462` and the slot as `reserved`.
- Cause on my side: the retrigger and the restarts. The cause in the system: a `held_uncertain` warm slot always requests an interim standby on the other provider, and nothing in the task disabled that path (policy was not changed, as instructed).

## Root causes found and fixed (each has a test)

1. Bootstrap role asserted Debian 12 only (now 12 or 13; test for trixie). `inventory/cloud_app.py` selected `s.slot_key` (column is `key`), had no fallback to the Python with `psycopg`, and had no `management` group, so the `ca` role could not delegate signing to management (now a `management` host from the inputs, pinned by `mgmt_known_hosts`).
2. Runner image had no `tf-slot.sh`, no Terraform slot roots and no state mount: every apply ended `terraform_not_started`. Now baked in; state dir `/var/lib/drproj/terraform` mounted, owned by uid 103.
3. The runner could not read the inputs file (root 0600) and `cloud_vm_key` was a 0-byte placeholder; the mgmt role now chowns the inputs copy and installs the `drproj` key once `providers.env` exists, and recreates the runner (a replaced bind-mounted file is invisible to the running container).
4. App role read the Supabase CA from a path the runner did not have (second mount added).
5. `standby_classify` and `wait_ready` had no real-adapter path: readiness was probed without the instance address and compared against simulation-only digests. Both now use the instance address and the recorded digests (`wait_ready` also accepts the readiness `version` that matches the primary's digest before the deploy is recorded).
6. `plan_slot(backup)` never set the address of the pre-declared backup line (it stayed 0.0.0.0 and could never go UP); `apply_routing` did not look up the slot address; the HAProxy client rejected HAProxy's normal `IP changed from … to …` answer to `set server addr`.
7. `standby_verify` read HAProxy once, 4 s after the routing change; it now waits up to 45 s for the backup to be UP.
8. Terraform creates state files 0700; `TfRunner.tighten` sets 0600/0700 after every run.
9. Reconcile opened `provider.unconfigured:*` but never resolved it; it now resolves the incident when the provider scans OK.
10. Cloud node_exporter used a random per-host basic-auth password while Prometheus holds one; cloud VMs now adopt the shared scrape password, and the stack is recreated when its web config is replaced.
11. File_sd scrape targets existed only for the primary; the mgmt role now writes `cloud-<slot>.json` for bound cloud instances (regenerated on each run of the play; files of released slots are not removed automatically).
12. Reconcile ignored a `reserved` slot whose owner operation had already ended; the Prometheus freshness gate could not parse nanosecond timestamps (every real scale-in/scale-out gate would have failed); scale-in tried to drain a slot routed `none` (see the adoption section).

## Mistakes during the run (recorded, not hidden)

- The `scp` of the workstation `deployment-inputs.local.json` replaced management's copy. A diff against the other management copy (`/opt/drproj/src/drproj-mgmt/deploy/inputs/`) showed only my intended edits, so nothing was lost.
- The size availability gap in the preflight (above).
- Runner container recreations and `mgmt.yml` runs while alert-driven repairs were in flight killed those workflows (`workflow_error`) and left the Linode VM above.

## Orphan adoption and release (T29), executed 15:26-15:46Z

The Linode VM `109576462` (tags `project:cdr`, `slot:interim-warm-linode`, `op:812b260d…`, `attempt:1`) had no Terraform state and its slot was `reserved` by an operation that had already failed. The operator approved adoption (not a hand delete). Steps, as run:

1. **Ownership** confirmed from the provider tags and the operation record. A direct provider delete was attempted first and refused by the session's permission check; it was not retried.
2. **Reconcile gap found.** Reconcile treated a `reserved` slot as dead only when its operation was non-terminal, so a slot reserved by an operation that had already ended was never touched. Fixed (such a slot is marked `held_uncertain` and resolved with provider evidence; test `test_reconcile_holds_and_binds_a_reserved_slot_whose_operation_already_ended`).
3. **`terraform import`** from the runner as uid st2, into the slot's own state: `terraform init -reconfigure -backend-config=path=…/linode/interim-warm-linode/terraform.tfstate` in `infra/terraform/slot-linode`, then `terraform import` of `module.linode_instance[0].linode_instance.this` with id `109576462` and the original `op`/`attempt`/`slot` variables (state backed up first as `backups/pre-import-…`). A plan with `exists=true` then wants to replace the instance (`image` and `authorized_keys` are not importable on Linode); that is irrelevant for the release, which applies `exists=false` (destroy only). State files stay 0600.
4. **Bind via reconcile (about 15:30Z):** `held_uncertain → bound`, one resource matching a recorded attempt of that slot (`ops.resolve_held_slot` with provider evidence, attempt row `started → failed`, no delete issued).
5. **Release by the designed path.** With `warm` bound and ready, reconcile requests `scale_in` of the surplus interim slot (`ops.scale_in_candidate()` prefers it). Three attempts died on bugs that real data exposed for the first time, each fixed with a test: `de7be41c` (the Prometheus freshness check could not parse the nanosecond `lastScrape`), `e0d884bb` (an interim slot routed `none` cannot go to `draining`; scale-in now skips the drain for a slot routed `none`), `0cd41f2b` (started before the sync of that fix). Attempt `41bd6f5c` succeeded: 15:44:53 queued, lease at generation 222 at 15:45:09, `begin_slot_release`, `run_heavy terraform_apply exists=false`, `delete_slot_resource` (provider `get` absent and `list_by_tag` empty), `release_slot`, event `VM deleted and absence confirmed through the provider API; slot released` at 15:46:13, `operation.succeeded` 15:46:15 (82 s).
6. **After:** Linode instances 0, slot `interim-warm-linode` free, Terraform state for it held no resources and its directory was removed, and the five Linode incidents were resolved with `cdr.resolve_incident` (`orphan:linode:109576462`, `standby.repair_failed:interim-warm-linode`, `provision.exhausted:627a3ae2…`, `…43337583…`, `…943fb995…`).

## Incidents

Resolved by reconcile: `provider.unconfigured:digitalocean`, `provider.unconfigured:linode`. Resolved through `cdr.resolve_incident`: `standby.uncertain:warm`, `standby.repair_failed:warm`, `provision.uncertain:warm` (warm standby healthy), and the five Linode incidents above. None open.

## Open items

1. No clean from-empty run to measure the RTO (T28 stays partial).
2. A held `warm` slot still makes the stack build an interim standby on the other provider by itself. That is the designed behavior, but it means a failed first attempt can create a VM on the second provider (docs/15 section 8a, step 5).
3. The runner image digest was bumped six times; the final pin is in `config/deployment-inputs.example.json` and the management copy. Cloud file_sd targets are regenerated by the `mgmt` role, not by a workflow, and files of released slots are not removed automatically.
