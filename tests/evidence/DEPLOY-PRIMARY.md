# DEPLOY-PRIMARY evidence: relief app on the primary

Run from the management VM (`/opt/drproj/ansible`, root) against primary `103.141.79.171`, 2026-10-27 ~22:10-22:35Z. No secrets appear here: the database URL came from `/etc/drproj/secrets/db-app_runtime.env` into the shell environment, `/etc/drproj/app/.env` on primary is 0600, and the node_exporter password was read over SSH without being printed.
Status: **done** (one accepted gap: metrics ports are not source-restricted, see below).

## Changes made for this task

- drproj-app `ec3c2b1`: `compose.yaml` sets `NODE_EXTRA_CA_CERTS=/etc/drproj/ca/supabase-root-2021.crt` and mounts `./ca/supabase-root-2021.crt`; `ops/0014_register_fixed_instance.sql` adds `ops.register_fixed_instance(role, ip, digest)` (SECURITY DEFINER, `primary` only, idempotent upsert, EXECUTE for `dr_automation` only).
- drproj-mgmt `258a0ae`: app role copies the Supabase root CA to `/etc/drproj/app/ca/`, renders `DEMO_MODE` from `app_demo_mode`; new `inventory/group_vars/primary.yml` sets `instance_id: primary`, `app_demo_mode: true`. `APP_DB_POOL_MAX` stays 2. `test_env_parity` checked by hand (vendored compose equals the source minus its header line); pytest is not installed on the workstation or management.
- Migration 0014 applied with `db-migration.env` ("Applied: ops/0014_register_fixed_instance.sql").

## Ansible

- `app.yml --check --diff --limit primary-vm`: reached "Start the stack" with 4 changes (CA directory and file, compose, `.env`); that task failed only because check mode never creates `compose.yaml`. Expected, not a defect.
- Real run: `ok=37 changed=5 failed=0`. The play re-issued the primary leaf (CN now `primary`, SAN unchanged), then passed its own liveness, readiness, identity-header and digest assertions.
- `inputs_file` did not resolve by default on management in this session (the `management_inputs_file` default was not picked up), so `-e inputs_file=/opt/drproj/ansible/deployment-inputs.local.json` was passed. Cause not investigated.

## Verification (from management, `--cacert /etc/drproj/ca/ca-bundle.crt`)

| Check | Result |
|---|---|
| Container image id / `RepoDigests` | `sha256:4427a3a7…eaea` / `ghcr.io/obedsayyad/drproj-relief@sha256:4427a3a7…eaea` |
| Non-root | `Config.User=node`, `id -u` = 1000 |
| `.env` | mode 600 root; relief and node_exporter containers `healthy` |
| `GET :8443/health/live` | 200 |
| `GET :8443/health/ready` | 200, `maintenance:false`, `maintenance_generation:0` |
| Headers | `X-Instance-Id: primary`, `X-App-Version: 4427a3a781ed` |
| `GET /api/requests` | 12 seeded rows before the test row |
| `POST /api/requests` + `Idempotency-Key` | 201; same key again: 201 with `Idempotent-Replayed: true` (response bytes differ from the first, which was not investigated) |
| `PATCH` `{"status":"in_progress","expected_version":1}` | 200, `status=in_progress`, `version=2` |
| `:9100/metrics` over TLS | 200 with basic auth, 401 without |
| `http://:9464/metrics` | 200, 37 metric families |

The verification POST left one extra row ("DEPLOY-PRIMARY verification", now `in_progress`); the table holds 13 rows.

## Registration

- `ops.register_fixed_instance('primary','103.141.79.171',<digest>)` as the `automation` login: second call returned the same id; `'warm'` was rejected (`CDR04 bad_argument`).
- As the `ops` login: one row, `self_hosted` / `primary` / `ready` / routing `none` / `simulated=false`, digest `sha256:4427a3a781ed…`, `public_ip` and `last_seen_at` set.
- `GET /api/instances` through the management API was not run: the API is not deployed on management yet. It reads this same table (and redacts `public_ip` by design).

## Accepted gaps

- Ports 8443, 9100 and 9464 are reachable from any source; the relief metrics server binds all interfaces. Per the operator, no firewalling now; revisit at the end if requested (docs/12 accepted risk).
- GHCR pulled anonymously (packages are public); no registry login was used.
