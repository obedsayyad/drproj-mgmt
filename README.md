# drproj-mgmt

Management repository (`obedsayyad/drproj-mgmt`): pnpm workspace (Node 22+, TypeScript strict, Vitest).
`docs/` is the `drproj-docs` bundle (git submodule; a local symlink to `../drproj-docs` until the repos are pushed).

Packages: `apps/operations` (management API + recovery dashboard; see its README), `packages/contracts` (types generated from `docs/spec`), `tests/integration`.
Later phases add `infra/terraform`, `infra/ansible`, `automation/stackstorm`, `deploy/compose/mgmt`, `monitoring` and `tests/load` here (docs/docs/11, GitHub repositories).

Database migrations live in `drproj-app` (one ordered runner: roles, `ops`, then `app`). This repo's integration tests read the SQL directly from `../drproj-app/database/migrations/sql` (never a copied install), so `drproj-app` must be checked out next to this repo (CI does the same). A hash-compare test fails if a stale installed copy of the SQL exists.

## Commands (repo root)

| Command | Purpose |
|---|---|
| `pnpm install` | install (pnpm 10.30.3) |
| `pnpm gen:api` | regenerate `packages/contracts/src/generated/openapi.ts` from `docs/spec/openapi.yaml` |
| `pnpm typecheck` | `tsc --noEmit` in every package |
| `pnpm test` | unit tests (contracts, policy validation) |
| `pnpm test:integration` | real-Postgres operations API tests (T11, T24, T31); needs `TEST_DATABASE_URL`, skipped otherwise |
| `pnpm build:web` | build the dashboard into `apps/operations/web/dist` |
| `pnpm seed:policy` | create policy version 1 from `docs/config/policy-defaults.json` via `SUPABASE_OPS_URL` (once) |
| `pnpm start:ops` | run the management API + dashboard (env in `apps/operations/README.md`) |

Single test file / single test:

```
pnpm vitest run apps/operations/src/policy.test.ts
pnpm vitest run apps/operations/src/policy.test.ts -t "cross-field"
```

Local integration database: same disposable Postgres as in `drproj-app/README.md`; run `pnpm migrate` there first or let the tests apply migrations.

Environment variable names are in `.env.example` (no values).

## Compose stacks (Phase 3.1)

- `deploy/compose/mgmt` (management VM) and `deploy/compose/testing` (k6). Names-only `.env.example`, secret-file layout in `deploy/compose/mgmt/secrets/README.md`, host notes (swap, directories) in `deploy/compose/mgmt/HOST-NOTES.md`. Images: `apps/operations/Dockerfile` (build from this repo root, local only) and the StackStorm runner image `automation/runner/Dockerfile` (pinned Terraform with an offline provider mirror, ansible-core, PostgreSQL client).
- `scripts/check-perms.sh [PATH...]` fails unless `.env`/secret files are 0600 and their directories (and the backup and Terraform-state directories) are 0700.
- `tests/compose/t31.sh` is the local T31 run (Docker Desktop, not the management VM): compose hardening assertions plus 401/200 checks through Caddy; output in `tests/compose/t31.local.out`.
- Ops `/metrics` is on `OPS_METRICS_PORT` (default 9465), Compose network only.
