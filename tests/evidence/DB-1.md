# DB-1 evidence: Supabase initialisation

Run from the management VM (Docker: `postgres:17-alpine` for psql, `node:22-bookworm-slim` for the repos), 2026-10-27 ~22:00Z. No secrets appear here; passwords live only in `/etc/drproj/secrets/db-*.env` (0600). Helper scripts: `scripts/db-init/` (mirrored in `/opt/drproj/scripts/`).

## Server and pooler

- `SELECT version()` → PostgreSQL 17.11 (aarch64). `postgres_server_major` = 17 recorded in the example and local inputs (value only) and docs/12.
- Session pooler `aws-0-ap-southeast-1.pooler.supabase.com:5432` reachable; `postgres` is not a superuser (rolsuper f, rolcreaterole t).
- Node needed Supabase's root CA (chain: wildcard → Supabase Intermediate 2021 CA → Supabase Root 2021 CA). Downloaded root SHA-256 matched the one the pooler presents; installed as `/etc/drproj/ca/supabase-root-2021.crt`, used through `NODE_EXTRA_CA_CERTS`. TLS verification was not disabled.
- No named prepared statements: `db.ts` in both apps notes it and no `query({name})` / `PREPARE` exists in app or migration code.

## Migrations (order: migrate, then logins)

Group roles come from `ops/0001` and `ops/0009`, so logins can only be bound after migrating. First run applied ops 0001–0013 and app 0001–0002 (15 files); second run: "No pending migrations." Database was empty beforehand (10 MB, no `dr_*` roles).

## Login roles

`app_runtime`→`dr_app_runtime`, `ops`→`dr_ops`, `backup`→`dr_backup`, `restore`→`dr_restore`, `automation`→`dr_automation`; 32-char passwords; env files `db-{app_runtime,ops,backup,restore,automation}.env` (0600) hold `SUPABASE_APP_URL`, `SUPABASE_OPS_URL`, `SUPABASE_BACKUP_URL`, `SUPABASE_RESTORE_URL`, `DR_AUTOMATION_URL`; `db-migration.env` holds the `postgres` string.

## Seeds

`seed:policy` → "policy version 1 seeded", re-run → "already initialized". Policy v1 status active. `seed` (DEMO_MODE intended on primary) → 12 inserted, re-run → 0. `ops.schema_migrations` 13 rows, `app.schema_migrations` 2.

## Per-role verification (all attempts rolled back)

- app_runtime: read/insert/update `app.relief_requests` OK; `ops.app_admit_write()` returns (maintenance f, generation 0); DDL in `app` denied; `ops.capacity_slots`, `ops.instances`, `ops.keepalive_runs`, `ops.runtime_flags` denied.
- ops: `ops.audit_events` insert OK; `ops.capacity_slots` write denied; `ops.acquire_lease()` EXECUTE denied; schema `app` denied.
- automation: reads `ops.policy_versions` (1 row) and capacity tables; direct write to `ops.capacity_slots` denied by design (writes go through SECURITY DEFINER functions); `ops.acquire_lease()` is executable (reached its logic: `operation_not_active` for a random uuid). Schema `app` denied.
- backup: `SELECT count(*) FROM app.relief_requests` = 12; insert denied; schema `ops` (incl. `app_admit_write`) denied.
- restore: owns schema `app` through `dr_restore` (`pg_namespace.nspowner` = dr_restore); create/drop table in `app` and `ALTER SCHEMA app RENAME` succeed in a rolled-back transaction; schema `ops` denied. No Supabase limitation found; `GRANT dr_restore TO postgres` worked.

## Keep-alive and size

`cdr` keepalive action logic (stub `Action`, psycopg, ops URL) → `(True, 'ok')`; `ops.keepalive_runs` has a `stackstorm`/succeeded row. StackStorm itself is not running yet, so this exercised the action code, not a scheduled rule. `pg_database_size` = 11.5 MB (budget 450 MB).
