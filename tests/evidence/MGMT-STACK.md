# MGMT-STACK evidence: management Compose stack via the `mgmt` Ansible role

Run on the management VM (`ob-project-vm-1`, `103.141.79.170`, 8 vCPU / 16 GB), `/opt/drproj/ansible` as root, 2026-10-28 ~09:50-10:30Z. No secrets appear here: the operator password is in `/etc/drproj/secrets/ops-operator.pw` (0600), every other generated secret is under `/etc/drproj/mgmt/` (0700); the Ansible logs, the source trees and the ops container log were grepped for the operator password, its hash and the `st2admin` password (0 matches).
Status: **done for this task** after MGMT-STACK-FIX (below): the stack runs on production certificates, T30/T31 pass, the startup reconcile ends noop (`providers_unconfigured`) and the one-heavy-job half of T18 is measured. The sections after this line are the original MGMT-STACK record; "Not done" item 1 and the T18 note are superseded by the fix section.

## Changes made for this task

- drproj-mgmt (this commit): `infra/ansible/roles/mgmt` implemented (preflight, secrets, render, up, verify, perms; templates for the Compose env, st2 credentials, pack config and the `st2-cli.sh` wrapper); `playbooks/mgmt.yml`; `compose.ansible.yaml` mounts the Supabase root CA into `ops` and the actionrunner and the node_exporter scrape inputs into Prometheus; `prometheus/scrape.d/node.yml` (job `node`, HTTPS + basic auth, loaded through `scrape_config_files`); `scripts/db-init/create-stackstorm-login.sh`; `.gitignore` for generated `monitoring/targets/*/*.json`.
- drproj-docs `efad7a1`: runbook 8 "Management stack bring-up", docs/08 defaults, docs/11 progress, docs/10 VM size.
- Database (Supabase): new login `stackstorm`, member of `dr_ops` and `dr_automation`, created once with the migration login (the pack needs one login in both groups). Existing logins unchanged.
- `ingress.acme_email` set to the ACME contact and `ingress.acme_staging` set to `false` in the local (gitignored) inputs on management and on the workstation.

## Ansible

- `mgmt.yml --check --diff`: passes the preflight, secrets and render tasks and stops at "Validate the rendered Compose project" (the env file does not exist before the first real run). Expected.
- Real runs, in order: run 1 failed at "Start the stack" (st2 services could not read `st2.user.conf`, `root:root 0600` against container uid 103); run 2 failed at the final `/api/overview` check (ops: `SELF_SIGNED_CERT_IN_CHAIN` to Supabase, root CA not mounted); run 3 `ok=42 changed=4 failed=0` (staging certificates); run 4 `ok=42 changed=5 failed=0` (production); run 5 `ok=43 changed=4 failed=0` (after the `stackstorm` login). Both defects were role/compose defects, fixed once each. The first failure left the st2 services crash-looping under `restart: unless-stopped`; I stopped them (`compose stop`) before fixing, no repeated restarts.
- `getent` is used for DNS because `dig` is not installed on management. docker modules need the Docker SDK that the controller venv lacks, so the role uses the Docker CLI instead.

## Verification

| Check | Result |
|---|---|
| Preflight | all four hostnames resolve to `103.141.79.170`; 80/443 free; `db-{ops,automation,backup,restore}.env` 0600 |
| Staging certificates | issuer `(STAGING) ... YE2` on ops, grafana, stackstorm and stats |
| Production certificates | issuer `Let's Encrypt YE2` on all four, notAfter 2027-01-26 |
| `GET /api/overview` | 200; `supabase` fresh, `stackstorm` fresh, `prometheus` fresh, `haproxy` unavailable ("not probed in this phase"), `providers` unavailable ("no successful reconcile run recorded yet") |
| `GET /api/instances` | one item, `role: primary`, `provider: self_hosted`, `simulated: false`, `public_ip` redacted |
| T31 without credentials | POST, PATCH, PUT, DELETE all 401 with `WWW-Authenticate: Basic realm="restricted"` |
| T31 with credentials | the same four methods on a nonexistent path return 404 (not 401); a wrong password returns 401; GET stays 200 |
| Grafana | `/` redirects to `/login`; `/api/search` without login is 401 (anonymous off) |
| StackStorm | the UI shell loads, `/api/v1/actions` without a token is 401; `st2admin` password replaced (generated htpasswd) |
| Prometheus and Alertmanager from the workstation | `:9090` and `:9093` connection refused (no published ports); `:8080`, `:9465`, `:27017`, `:5672` unreachable |
| Prometheus targets | `relief` (primary `:9464`, HTTP), `node` (primary `:9100`, HTTPS, project CA, basic auth), `ops`, `prometheus`, `alertmanager`: all `up` |
| T30 size alert | `CdrSupabaseDbSizeHigh` loaded (inactive); 11 rules loaded in total. `CdrSupabaseKeepaliveStale` fired until the first keep-alive and cleared afterwards |
| Keep-alive (T30) | `st2 run cdr.keepalive` -> `succeeded`; new row in `ops.keepalive_runs` (`caller=stackstorm`, `ok=t`). Before the `stackstorm` login the same action failed with `record_failed:privilege` |
| St2 rules | `cdr.keepalive_every_6h` enabled, `cdr.reconcile_every_60s` disabled, `cdr.cdr_alert` enabled |
| `promtool`/`amtool` (workstation) | `tests/compose/promtool.sh`: config (including the re-rendered policy_version file), 11 rules, rule tests and `amtool check-config` pass |
| File modes | `/etc/drproj/mgmt` 0700, `mgmt.env` 0600, secret files 0600 (owner = reading container user), `st2_api_key` 0640 |

### T18 snapshot (`docker stats --no-stream`, idle, ~25 min after start, no heavy job)

20 containers use about 1577 MiB in total against a summed `mem_limit` of 3568 MiB (the VM has 16 GB; `free -m`: 13.5 GB available, 179 MB of 2 GB swap used). Largest: grafana 252/256 MiB (98% of its ceiling, do not lower it), mongo 152/320, rabbitmq 135/256, st2actionrunner 116/512; each other st2 service 67-91 of 160-192 MiB; ops 57/160; prometheus 70/256; caddy 21/64; alertmanager 30/64; redis 8/64; blackbox 7/48; st2web 8/64. The one-heavy-job half of T18 is not measured here.

## Not done / accepted gaps

- **Startup reconcile does not end noop.** `st2-startup-reconcile` was run twice (once with the `automation` login, once with `stackstorm`). Both executions show `succeeded` in StackStorm, but `ops.operations` records them `failed / workflow_error`: `cdr.reconcile_pass` raises an adapter configuration error at the provider scan (reported as `provider_adapter_missing_or_unknown`) because the real adapter has no provider tokens. No provider API call is possible without tokens and none appears in the child executions (`open_operation`, `adapter_info`, `update_ops_state`, `control_state`, `acquire_lease`, `reconcile_pass`, `fail_operation`). So the 60 s timer stays disabled. Whether a provider without a token should be skipped (docstring: "a provider that does not answer is skipped") so that reconcile ends noop is a pack decision left open.
- Before the `stackstorm` login, seven `cdr.alert_intent` executions failed with `record_failed:privilege`; since then they succeed.
- `stats.cproj.obed.one` returns 503 (no HAProxy upstream yet). Blackbox has no probe targets and `cdr_member_serving` has no producer in this repo, so the cert-expiry and serving-member rules have nothing to evaluate.
- Provider tokens, region/size/image and a cloud VM key do not exist: the pack config holds `REPLACE_ME_BEFORE_PROVISIONING` placeholders, the cloud VM key file is empty, and provisioning is impossible until they are supplied.
- The role is idempotent apart from re-rendered files; the `ops-operator` password is generated by the role (24 characters) because `admin@1234` is shorter than the API's 12-character minimum.
- Metrics ports on the primary stay unrestricted (accepted in DEPLOY-PRIMARY); nothing was firewalled.

## MGMT-STACK-FIX (2026-10-28, 11:00-11:20Z)

Code: drproj-mgmt `a19cbc7` and `a7d7a9b`, drproj-docs `77330d3`. Deployed by cloning both public repos on management, `rsync` of the source into `/opt/drproj/src/drproj-mgmt` and `infra/ansible` into `/opt/drproj/ansible` (runbook 8, step 1; the workstation has no SSH key for the VM), then `ansible-playbook playbooks/mgmt.yml --limit management-vm`: `ok=43 changed=5 failed=0` (Grafana recreated, pack config re-rendered with `backup_db_url`/`restore_test_url`, actionrunner recreated). A later workflow-only fix was copied into the source tree and registered with `packs.load register=actions packs=cdr`. No secrets in this file; no cloud resource was created or contacted (no provider tokens exist).

### 1. Reconcile with unconfigured providers
- Real adapter: a provider whose token variable is unset answers `unavailable` / `provider_unconfigured` (observe, get, list_by_tag, power_on) with no HTTP call and no exception. `reconcile_pass` skips that provider's tag scan, opens ONE `provider.unconfigured:<provider>` warning incident (the existing fingerprint check keeps a recurring condition to one), and with both unconfigured reports `noop_reason=providers_unconfigured`; `workflows/reconcile.yaml` ends that case at step `noop` instead of `succeed`.
- Startup reconcile re-run on management (`cdr.reconcile`, execution `6ae1d6b2...`): `ops.operations` row `kind=reconcile status=succeeded current_step=noop`, `noop_reason=providers_unconfigured`, `providers_unconfigured=[digitalocean, linode]`, no orphans, resumes or errors (the 10:12 run stays `failed/workflow_error` as history).
- `GET /api/incidents` lists both `provider.unconfigured:digitalocean` and `provider.unconfigured:linode` (warning, open, source `reconciliation`). The 60 s timer rule stays disabled; enabling it was not asked.
- Pack tests (python:3.10, local Postgres 17, `pg_dump` 17, Orquesta 1.6.0, `KEEPALIVE_TEST_URL` set): 414 passed, 0 skipped. New: missing token makes no HTTP call and is never absence (`test_real_adapter.py`); both-missing gives noop plus one incident each and a second pass adds none; one-missing scans the other provider only (`test_p43_actions.py`); the reconcile workflow ends `noop_unconfigured` only on `noop_reason` and releases the lease either way (`test_workflow_failure_paths.py`).

### 2. Grafana ceiling
`mem_limit` 256m to 384m (`docker inspect` Memory = 402653184, container healthy after the re-apply). The always-on sum is now 3696 MB (docs/08, compose header). During the probe window Grafana peaked at 324 MiB (84% of 384), up from 252 MiB idle before; it was freshly recreated, so part of that is warm-up.

### 3. T18, one heavy job beside the full stack
- Job: `cdr.restore_probe` (new; a lease-holding `reconcile`-kind operation, because `ops.open_operation` accepts no other kind until a P7 migration) runs `cdr.run_heavy` task `restore_probe` under `policies/heavy.yaml`: `pg_dump -Fc -n app` of the live app schema with the backup login, `pg_restore --no-owner --no-privileges --exit-on-error` into a scratch database on `restore-test-pg` (profile `restore-test`, started before the run and stopped and removed after; the named volume stays), counts compared, scratch database dropped, dump deleted. Credentials travel as `PG*` variables, not argv.
- Result of the measured run: operation `succeeded` / `done`, `tables_source=4 tables_restored=4 rows_source=31 rows_restored=31 dump_bytes=10036`; the `heavy` task took 20 s, the whole workflow about 50 s. `restore-test-pg` was healthy before the run.
- First attempt (execution window 11:08Z): `run_heavy` returned the same `ok` result, but the workflow's `succeed` task failed with `invalid_transition` (it must pass through `verifying`, as reconcile does) and the operation ended `failed/workflow_error` (id `29d8dae9...`, kept as history). Fixed in `a7d7a9b` (a `verifying` step and a test that pins the order); the sampler of that attempt also wrote nothing (shell ordering), so its memory numbers are not used.
- `docker stats` sampled back to back for 33 s (373 samples, 22 containers incl. `restore-test-pg` and the one-off `st2-register` the CLI wrapper starts): sum of per-container peaks 1872 MiB, largest single sampling tick 1824 MiB, against 16 GB. Peaks: grafana 324/384, rabbitmq 210/256, mongo 150/320, st2actionrunner 136/512 (pg_dump and pg_restore run in it), st2auth 94/192, st2api 93/192, prometheus 92/256, st2stream 90/160, st2workflowengine 86/160, `restore-test-pg` 33/512. Highest CPU: mongo 56%, rabbitmq 34%, actionrunner 36%.
- OOM: `dmesg` OOM/oom-kill count 0 before and 0 after; `docker events` (oom and die) over the window shows no oom event and no `die` with a non-zero exit code (the `die` lines are the CLI's one-off `st2-register` containers and the stopped `restore-test-pg`, all `exitCode=0`). `free -m`: swap used 37 MB before and after; available memory 13.3 GB before, 13.2 GB after.
- Limits of this measurement: the app schema holds 4 tables, 31 rows and a 10 KB dump, so it shows the stack tolerates the heavy-job machinery and a tiny restore at once; it does not show behaviour near the 450 MB restore preflight limit. Repeat with the P7 backup/restore on realistic data.

### 4. Operator password
Unchanged: the generated 24-character password in `/etc/drproj/secrets/ops-operator.pw` stays (no new password was supplied).
