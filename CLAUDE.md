# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

This is `obedsayyad/drproj-mgmt` (public; never commit secrets): the management API and recovery dashboard (`apps/operations`), `packages/contracts` (its own types generated from `docs/spec`) and `tests/integration`; later phases add `infra/terraform`, `infra/ansible`, `automation/stackstorm`, `deploy/compose/mgmt`, `monitoring` and `tests/load`. `docs/` is the `obedsayyad/drproj-docs` bundle as a git submodule (locally a symlink to `../drproj-docs`). Database migrations live in `drproj-app`; integration tests read the SQL directly from `../drproj-app/database/migrations/sql` (repos checked out side by side) and never from a copied `node_modules` install; `tests/integration/migrations-fresh.test.ts` hash-compares any installed copy against that tree and fails if stale. Plan: docs/docs/11-build-and-demo-plan.md, GitHub repositories.

Read `docs/CLAUDE.md` (the full implementation brief) first. Commands are in `README.md`: `pnpm typecheck`, `pnpm test`, `pnpm test:integration` (needs `TEST_DATABASE_URL`), single test via `pnpm vitest run <file> -t <name>`.

Terraform/runner checks (Docker only; never `init`/`plan`/`apply` against a real provider): from `infra/terraform`, `docker run --rm -v $PWD:/w -w /w[/slot-do|/slot-linode|/modules/<m>] hashicorp/terraform:1.17.0 fmt -recursive -check | init -backend=false && validate | test`; `docker run --rm -v $PWD:/data -w /data ghcr.io/terraform-linters/tflint:v0.55.1 --recursive`; `docker run --rm -v $PWD:/code -w /code bats/bats:1.11.1 tests/infra`; runner image: `docker build --platform linux/amd64 -f automation/runner/Dockerfile -t drproj-runner:local .`. Terraform runs go through `scripts/tf-slot.sh`. Pack tests (P5.2): `python:3.10` with `psycopg[binary]==3.3.6 requests==2.32.3 pyyaml responses==0.25.3 jsonschema pytest` plus Orquesta (see the pack README), `TEST_DATABASE_URL` and `KEEPALIVE_TEST_URL` set, `python -m pytest tests -q -rs` in `automation/stackstorm/packs/cdr` (0 skips); `docker run --rm -v $PWD:/mnt koalaman/shellcheck:stable scripts/tf-slot.sh`.

## Project

College demonstration of cloud compute allocation and disaster recovery for a sample disaster-relief request app. HAProxy routes app traffic to healthy servers; StackStorm orchestrates failover, scaling, and reconciliation; Terraform provisions elastic cloud VMs; Ansible configures machines and deploys Docker Compose stacks; Supabase Free PostgreSQL holds app and ops data; Prometheus/Grafana monitor; k6 load-tests.

Planned stack: TypeScript (pnpm workspaces per repo), React/Vite frontends, Node.js/Express APIs, node-postgres (bounded pools), DB migrations, Docker Compose.

## Hard constraints (do not violate)

- No AWS, Kubernetes, VPN, firewall setup/automation, Restic, PITR, Pulumi, or AWX.
- No login, Supabase Auth, accounts, or RBAC. The relief app has no login; dashboard views are public; mutating operations require the shared operator password (Caddy Basic Auth). Still validate allowed commands/parameters; never expose an arbitrary shell or Terraform path in the public API. Keep machine credentials out of browsers, source, logs, and API output.
- StackStorm is the sole automation controller; Node.js APIs must not become a competing infrastructure controller.
- Cloud application VM cap: up to 5 DigitalOcean + up to 5 Linode (`capacity.max_cloud_vms_per_provider`, immutable ceiling 5 each), counting standby, provisioning, uncertain, and cleanup-pending. Exactly 1 warm standby on either provider (`capacity.warm_provider`, default DigitalOcean). Scale out/in 1 VM per step, alternating providers. Fixed primary/management/LB/test VMs are self-hosted and excluded.
- Backups: app-schema `pg_dump` daily 00:00 UTC, 30-day retention, unencrypted at rest (dir 0700, files 0600, management VM only). Restores are operator-started: isolated validation first, then live app-only restore under maintenance. Ops records and maintenance controls live outside the restored schema.
- Terraform state is separate per provider/failure boundary and per slot; never global `terraform destroy` for scale-in.
- Don't invent IPs, credentials, registry namespaces, or account IDs. Keep real integrations distinct from simulation; never present fake metrics or mock provisioning as real.
- Don't deploy cloud resources unless deployment is part of the request.

## Key invariants (summary — full list in `docs/CLAUDE.md`, the full implementation brief; read it first)

Reserve a cloud slot transactionally before provisioning and hold it until deletion is confirmed; a timed-out API call doesn't prove a VM doesn't exist (reconcile before retry); failover, overload, retry and reconciliation share one capacity lock with fencing generation; stale metrics or missing control state never authorize scaling/deletion; all writes (including direct-IP and background) honor maintenance mode; only project-tagged resources are eligible for cleanup.

## Workflow

Follow the phase gates in `docs/docs/11-build-and-demo-plan.md`: contracts/app data → management API/UI → Compose/monitoring → StackStorm workflows → Terraform/Ansible → HAProxy routing → backups/restore → end-to-end evidence. Operator runbooks: `docs/docs/15-runbooks.md`. Run targeted, meaningful checks at each gate; don't write tests that mirror constants. Prefer reasonable defaults over questions; ask only for genuinely missing deployment inputs. Update the docs when implementation resolves an engineering default.

## Response Contract

Reply ONLY in this shape, ≤ 25 lines, no code dumps, no file contents:

```text
STATUS: DONE | BLOCKED | PARTIAL | NEEDS_DECISION
TASK: <task id given to you>
CHANGED: <paths, one per line, with 3–6 word summary each>
CHECKS: <command → pass/fail, one per line; "none run" if none>
DECISIONS: <engineering defaults you chose, one per line; "none">
QUESTIONS: <numbered, only if STATUS=NEEDS_DECISION/BLOCKED; else "none">
NEXT: <what you'd do next if told "continue", 1 line>
```

Rules: never paste diffs/logs; refer to files by path. If a doc in `docs/` conflicts with `CLAUDE.md`, say so in `DECISIONS`, don't resolve it silently.
