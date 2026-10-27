# apps/operations

Management API (Express 5, node-postgres) and recovery dashboard (React 19 + Vite, in `web/`). Phase 2 of docs/docs/11.
Part of `drproj-mgmt` (docs/docs/11, GitHub repositories). Commands below run from the `drproj-mgmt` root.

## Run

```
SUPABASE_OPS_URL=... pnpm seed:policy                  # once: policy version 1 from docs/config/policy-defaults.json
SUPABASE_OPS_URL=... OPS_OPERATOR_PASSWORD=... pnpm start:ops   # or OPS_TRUST_PROXY_AUTH=true instead of the password
pnpm --filter @drproj/operations build:web            # dashboard into web/dist; served at /
pnpm --filter @drproj/operations dev:web              # dashboard dev server; proxies to OPS_API_ORIGIN (default http://127.0.0.1:8080)
```

## Configuration (environment)

| Variable | Default | Notes |
|---|---|---|
| `SUPABASE_OPS_URL` | required | Login user in `dr_ops`; session pooler |
| `OPS_PORT` | 8080 | Plain HTTP on the Compose network; Caddy terminates TLS for ops.cproj.obed.one |
| `OPS_DB_POOL_MAX` | 3 | Integer 1..5; other values make the API refuse to start |
| `OPS_OPERATOR_PASSWORD` | required | Same shared password Caddy checks (user `operator`, min 12 chars). Enforced on every mutation, so the container port cannot bypass Caddy. The API refuses to start without it unless `OPS_TRUST_PROXY_AUTH=true` |
| `OPS_TRUST_PROXY_AUTH` | `false` | Set `true` only to run without `OPS_OPERATOR_PASSWORD` and rely on Caddy basic auth alone; logs a warning at start |
| `ST2_API_URL`, `ST2_API_KEY`, `ST2_PACK` | unset, unset, `cdr` | StackStorm executions API. Unset: operations are recorded and wait, visibly |
| `PROMETHEUS_URL`, `HAPROXY_STATS_URL` | unset | Not probed in this phase; the overview reports them unavailable ("not probed in this phase") |
| `OPS_MAX_SSE_STREAMS`, `OPS_SSE_POLL_MS` | 20, 2000 | Progress streams |
| `OPS_WEB_DIR` | `web/dist` | |

## Behaviour decided in implementation

- Every change (POST/PATCH) is an operator intent: it becomes a queued `ops.operations` row (idempotent on `Idempotency-Key`), is audited with actor `operator`, and is handed to StackStorm (`<pack>.<kind>`, parameter `operation_id`). The API never sets an operation to running; only automation does.
- If StackStorm is not configured or the call fails, the operation stays `queued` with step `awaiting_automation` and an event saying so.
- Policy edits: deep-partial patch merged into the active policy, validated (schema plus the docs/03 cross-field rules), saved as a `pending` version. One pending version at a time. Version numbers keep increasing past failed versions. The `apply_policy` workflow finishes it with `ops.finish_policy_activation(version, success, result)`: success supersedes the old version; failure keeps the old one active.
- Problem codes: `POLICY_INVALID` (422, field errors), `VERSION_CONFLICT`, `POLICY_ACTIVATION_IN_PROGRESS`, `OPERATION_CONFLICT` (409), `RESTORE_NOT_VERIFIED`, `RESTORE_SIZE_UNKNOWN`, `RESTORE_INSUFFICIENT_HEADROOM`, `BACKUP_NOT_CAPTURED`, `TEST_BOUNDS_EXCEEDED` (422), `TEST_NOT_RUNNING` (409), `UNAUTHORIZED` (401), `POLICY_NOT_INITIALIZED` (503).
- Admission checks (not control): one backup, one verify-or-restore, one load test at a time. Load tests are capped by the contract (200 req/s, 600 s) and by the active policy's `demo` limits.
- Restore headroom uses `ops.backups.restored_bytes`, which `verify_backup` records after the isolated restore test.
- Progress stream: `GET /api/operations/{id}/events` sends each `ops.operation_events` row as a default SSE message whose data is a `spec/events.schema.json` envelope (`id` = durable sequence; `status`, `step`, `message` in `measurements`), replays after `Last-Event-ID`, and closes once the operation has finished.
- Integration status on the overview is observed directly; providers are judged by the age of the last successful `reconcile` run.
- Public responses never include instance IPs, provider resource IDs, backup file paths or any configured secret.
- The dashboard keeps the operator password in memory only.
