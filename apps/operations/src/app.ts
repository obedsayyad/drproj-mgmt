import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import express, { type NextFunction, type Request, type Response } from 'express';
import type pg from 'pg';
import { z } from 'zod';
import { changedPaths, deepMerge, IDEMPOTENCY_KEY_HEADER, IdempotencyKey, PolicyConfiguration, PolicyPatch } from '@drproj/contracts';
import { operatorAuth } from './auth.js';
import { recordDispatch, type Dispatcher } from './automation.js';
import type { Config } from './config.js';
import { decodeCursor, encodeCursor, isUuid } from './cursor.js';
import { ts } from './db.js';
import { toEventEnvelope, type EventRow } from './events.js';
import { observeIntegrations } from './integrations.js';
import { acceptOperation, activePolicy, assertNoActive, requestHash, TERMINAL, type AcceptArgs } from './operations.js';
import { Code, HttpProblem, sendProblem } from './problem.js';

export interface Deps {
  config: Config;
  pool: pg.Pool;
  dispatcher: Dispatcher;
  webDir?: string | undefined;
}

const MB = 1024 * 1024;
export const RESTORE_HEADROOM_LIMIT_BYTES = 450 * MB;
const MAX_BODY_BYTES = 8 * 1024;

const OperationKind = z.enum(['activate_standby', 'scale_out', 'scale_in', 'primary_failure', 'failback', 'provision_and_deploy', 'standby_repair', 'reconcile', 'apply_policy', 'create_backup', 'verify_backup', 'restore_application', 'start_load_test', 'stop_load_test']);
const OperationStatus = z.enum(['queued', 'running', 'waiting', 'verifying', 'paused', 'reconciling', 'cancelling', 'cancelled', 'succeeded', 'failed']);
const OperationCommand = z.strictObject({
  kind: z.enum(['activate_standby', 'scale_out', 'scale_in', 'standby_repair', 'reconcile', 'failback']),
  reason: z.string().max(1000).optional(),
});
const RestoreInput = z.strictObject({ backup_id: z.uuid(), reason: z.string().max(1000).optional() });
const TestRunInput = z.strictObject({
  scenario: z.enum(['smoke', 'baseline', 'compute_pressure', 'sustained_surge', 'recovery']),
  rate_rps: z.int().min(1).max(200),
  duration_seconds: z.int().min(1).max(600),
});

const Page = z.object({ limit: z.coerce.number().int().min(1).max(100).default(20), cursor: z.string().optional() });
const fieldErrors = (e: z.ZodError) => e.issues.map((i) => ({ field: i.path.join('.') || '(body)', message: i.message }));
const invalid = (e: z.ZodError, code: (typeof Code)[keyof typeof Code] = Code.ValidationFailed, status = 400) =>
  new HttpProblem(status, code, status === 422 ? 'Policy validation failed.' : 'Request validation failed.', {}, fieldErrors(e));

function parse<T extends z.ZodType>(schema: T, value: unknown): z.infer<T> {
  const r = schema.safeParse(value);
  if (!r.success) throw invalid(r.error);
  return r.data;
}

function cursorOf(raw: string | undefined) {
  if (raw === undefined) return undefined;
  const c = decodeCursor(raw);
  if (!c) throw new HttpProblem(400, Code.ValidationFailed, 'Invalid cursor.', {}, [{ field: 'cursor', message: 'invalid' }]);
  return c;
}

/** Keyset page over (timestamp column, id) descending. `extra` filters use $3.. params. */
async function keysetPage<T extends { id: string }>(
  pool: pg.Pool,
  opts: { select: string; from: string; tsCol: string; where: string[]; params: unknown[]; limit: number; cursor?: string | undefined; sortKey: (row: T) => string },
) {
  const after = cursorOf(opts.cursor);
  const params = [...opts.params, after?.createdAt ?? null, after?.id ?? null, opts.limit + 1];
  const n = opts.params.length;
  const where = [...opts.where, `($${n + 1}::timestamptz IS NULL OR (${opts.tsCol}, id) < ($${n + 1}::timestamptz, $${n + 2}::uuid))`];
  const r = await pool.query<T>(
    `SELECT ${opts.select} FROM ${opts.from} WHERE ${where.join(' AND ')} ORDER BY ${opts.tsCol} DESC, id DESC LIMIT $${n + 3}`,
    params,
  );
  const items = r.rows.slice(0, opts.limit);
  const last = items.at(-1);
  return { items, next_cursor: r.rows.length > opts.limit && last ? encodeCursor({ createdAt: opts.sortKey(last), id: last.id }) : null };
}

const CONNECTION_ERRORS = new Set(['ECONNREFUSED', 'ECONNRESET', 'ETIMEDOUT', 'ENOTFOUND', 'EAI_AGAIN', '57P01', '57P03', '08000', '08001', '08003', '08006', '53300']);
const isDependencyError = (err: unknown) => {
  const e = err as { code?: unknown; message?: unknown };
  return (typeof e.code === 'string' && CONNECTION_ERRORS.has(e.code)) || (typeof e.message === 'string' && /timeout exceeded when trying to connect|Connection terminated/i.test(e.message));
};

export function createApp({ config, pool, dispatcher, webDir }: Deps): express.Express {
  const app = express();
  app.disable('x-powered-by');
  app.set('etag', false);
  let openStreams = 0;

  app.use((_req, res, next) => {
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('X-Frame-Options', 'DENY');
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'");
    res.setHeader('Cross-Origin-Resource-Policy', 'same-origin');
    res.setHeader('Cache-Control', 'no-store');
    next();
  });

  app.get('/health/live', (_req, res) => void res.json({ status: 'ok' }));
  app.use(operatorAuth(config.operatorPassword));
  // Raw 8 KB guard for every content type: by Content-Length, then by streamed bytes (chunked).
  // JSON is parsed below; any other type is drained through express.raw so it cannot bypass the cap.
  app.use((req, _res, next) => {
    if (Number(req.get('content-length') ?? 0) > MAX_BODY_BYTES) return next(new HttpProblem(413, Code.PayloadTooLarge, 'Request body exceeds 8 KB.'));
    next();
  });
  app.use(express.raw({ type: (req) => !req.headers['content-type']?.toLowerCase().startsWith('application/json'), limit: MAX_BODY_BYTES }));
  app.use(express.json({ limit: MAX_BODY_BYTES, strict: true }));

  app.get('/health/ready', async (_req, res) => {
    try {
      await pool.query('SELECT 1 FROM ops.policy_versions LIMIT 1');
    } catch {
      throw new HttpProblem(503, Code.DependencyUnavailable, 'Database is unreachable.');
    }
    res.json({ status: 'ready' });
  });

  // ---------- Reads (public) ----------

  app.get('/api/overview', async (_req, res) => {
    const policy = await activePolicy(pool);
    const [instances, slots, simulated, incidents, backup, integrations] = await Promise.all([
      pool.query<{ role: string; provider: string; lifecycle_state: string; routing_mode: string; n: number }>(
        `SELECT role, provider, lifecycle_state, routing_mode, count(*)::int AS n FROM ops.instances
         WHERE lifecycle_state <> 'deleted' AND NOT simulated GROUP BY 1, 2, 3, 4`,
      ),
      pool.query<{ provider: string; n: number }>(
        `SELECT s.provider, count(*)::int AS n FROM ops.capacity_slots s
         LEFT JOIN ops.instances i ON i.id = s.bound_instance_id
         WHERE s.state <> 'free' AND coalesce(NOT i.simulated, true) GROUP BY 1`,
      ),
      pool.query<{ n: number }>(`SELECT count(*)::int AS n FROM ops.instances WHERE simulated AND lifecycle_state <> 'deleted'`),
      pool.query<{ open: number; critical: number }>(
        `SELECT count(*) FILTER (WHERE state = 'open')::int AS open,
                count(*) FILTER (WHERE state = 'open' AND severity = 'critical')::int AS critical FROM ops.incidents`,
      ),
      pool.query(
        `SELECT id, ${ts('capture_finished_at')} AS capture_finished_at, verification_status,
                extract(epoch FROM now() - capture_finished_at)::int AS age_seconds
         FROM ops.backups WHERE capture_status = 'captured' AND NOT is_safety_backup AND expired_at IS NULL
         ORDER BY capture_finished_at DESC LIMIT 1`,
      ),
      observeIntegrations(config, pool, policy.configuration.coordination.reconcile_interval_seconds, policy.configuration.monitoring.stale_after_seconds),
    ]);
    const count = (f: (r: (typeof instances.rows)[number]) => boolean) => instances.rows.filter(f).reduce((s, r) => s + r.n, 0);
    const caps = policy.configuration.capacity.max_cloud_vms_per_provider;
    const occupied = Object.fromEntries(slots.rows.map((r) => [r.provider, r.n]));
    res.json({
      observed_at: new Date().toISOString(),
      active_policy_version: policy.version,
      capacity: {
        desired: { primary: 1, warm: policy.configuration.capacity.warm_instances, cap_digitalocean: caps.digitalocean, cap_linode: caps.linode },
        observed: {
          primary_ready: count((r) => r.role === 'primary' && r.lifecycle_state === 'ready'),
          warm_ready: count((r) => r.role === 'warm' && r.lifecycle_state === 'ready'),
          elastic: count((r) => r.role === 'elastic'),
          serving: count((r) => r.routing_mode === 'active'),
          occupied_digitalocean: occupied['digitalocean'] ?? 0,
          occupied_linode: occupied['linode'] ?? 0,
        },
        simulated_count: simulated.rows[0]?.n ?? 0,
      },
      integrations,
      incidents: incidents.rows[0] ?? { open: 0, critical: 0 },
      latest_backup: backup.rows[0] ?? null,
    });
  });

  app.get('/api/instances', async (req, res) => {
    const q = parse(
      Page.extend({
        lifecycle_state: z.enum(['reserved', 'provisioning', 'deploying', 'ready', 'draining', 'deleting', 'deleted', 'failed', 'unknown']).optional(),
        routing_mode: z.enum(['none', 'backup', 'active', 'draining']).optional(),
        provider: z.enum(['self_hosted', 'digitalocean', 'linode']).optional(),
      }),
      req.query,
    );
    // public_ip and provider_resource_id are never selected: redacted (null) in public responses.
    const page = await keysetPage<{ id: string; created_at: string }>(pool, {
      select: `id, role, provider, slot_key AS slot, simulated, NULL::text AS public_ip, NULL::text AS provider_resource_id, vcpu, memory_mb,
               ${ts('last_seen_at')} AS last_seen_at, routing_weight, slot_state, lifecycle_state, health_state, routing_mode,
               image_digest, ${ts('created_at')} AS created_at`,
      from: 'ops.instances',
      tsCol: 'created_at',
      where: ['($1::text IS NULL OR lifecycle_state = $1)', '($2::text IS NULL OR routing_mode = $2)', '($3::text IS NULL OR provider = $3)'],
      params: [q.lifecycle_state ?? null, q.routing_mode ?? null, q.provider ?? null],
      limit: q.limit,
      cursor: q.cursor,
      sortKey: (r) => r.created_at,
    });
    res.json({ ...page, items: page.items.map(({ created_at: _c, ...rest }) => rest) });
  });

  app.get('/api/policies', async (req, res) => {
    const q = parse(Page, req.query);
    const before = q.cursor === undefined ? null : Number.parseInt(Buffer.from(q.cursor, 'base64url').toString(), 10);
    if (before !== null && !(before >= 1)) throw new HttpProblem(400, Code.ValidationFailed, 'Invalid cursor.');
    const r = await pool.query(
      `SELECT version, status, activation_phase, configuration, apply_result, ${ts('created_at')} AS created_at, ${ts('activated_at')} AS activated_at
       FROM ops.policy_versions WHERE ($1::int IS NULL OR version < $1) ORDER BY version DESC LIMIT $2`,
      [before, q.limit + 1],
    );
    const items = r.rows.slice(0, q.limit);
    const last = items.at(-1) as { version: number } | undefined;
    res.json({ items, next_cursor: r.rows.length > q.limit && last ? Buffer.from(String(last.version)).toString('base64url') : null });
  });

  const opColumns = `id, kind, status, policy_version, current_step, stackstorm_execution_id, parameters,
    ${ts('updated_at')} AS observed_at, outcome AS result, ${ts('created_at')} AS created_at, ${ts('finished_at')} AS finished_at`;

  app.get('/api/operations', async (req, res) => {
    const q = parse(Page.extend({ status: OperationStatus.optional(), kind: OperationKind.optional() }), req.query);
    res.json(
      await keysetPage<{ id: string; created_at: string }>(pool, {
        select: opColumns,
        from: 'ops.operations',
        tsCol: 'created_at',
        where: ['($1::text IS NULL OR status = $1)', '($2::text IS NULL OR kind = $2)'],
        params: [q.status ?? null, q.kind ?? null],
        limit: q.limit,
        cursor: q.cursor,
        sortKey: (r) => r.created_at,
      }),
    );
  });

  app.get('/api/operations/:id', async (req, res) => {
    const id = req.params['id'] ?? '';
    const r = isUuid(id) ? await pool.query(`SELECT ${opColumns} FROM ops.operations WHERE id = $1`, [id]) : null;
    if (!r?.rows[0]) throw new HttpProblem(404, Code.NotFound, 'Operation not found.');
    res.json(r.rows[0]);
  });

  app.get('/api/operations/:id/events', async (req, res) => {
    const id = req.params['id'] ?? '';
    const exists = isUuid(id) ? await pool.query('SELECT 1 FROM ops.operations WHERE id = $1', [id]) : null;
    if (!exists?.rowCount) throw new HttpProblem(404, Code.NotFound, 'Operation not found.');
    if (openStreams >= config.maxSseStreams) throw new HttpProblem(503, Code.TooManyStreams, 'Too many open progress streams.', { 'Retry-After': '5' });
    const lastHeader = req.get('last-event-id');
    let last = lastHeader && /^\d{1,18}$/.test(lastHeader) ? Number(lastHeader) : 0;

    openStreams++;
    res.status(200).set({ 'Content-Type': 'text/event-stream; charset=utf-8', 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no' });
    res.flushHeaders();
    res.write('retry: 3000\n\n');
    let closed = false;
    let timer: NodeJS.Timeout | undefined;
    let idle = 0;
    const stop = () => {
      if (closed) return;
      closed = true;
      openStreams--;
      clearTimeout(timer);
      res.end();
    };
    req.on('close', stop);

    const tick = async () => {
      if (closed) return;
      try {
        const ev = await pool.query<EventRow>(
          `SELECT e.event_id::text, e.sequence::text, e.event_type, e.status, e.step, e.message, e.source,
                  ${ts('e.occurred_at')} AS occurred_at, e.operation_id::text, o.policy_version
           FROM ops.operation_events e JOIN ops.operations o ON o.id = e.operation_id
           WHERE e.operation_id = $1 AND e.sequence > $2 ORDER BY e.sequence LIMIT 100`,
          [id, last],
        );
        for (const e of ev.rows) {
          last = Number(e.sequence);
          // Default `message` event so clients receive every type; the data is an events.schema envelope.
          res.write(`id: ${last}\ndata: ${JSON.stringify(toEventEnvelope(e))}\n\n`);
        }
        if (ev.rows.length === 0) {
          const st = await pool.query<{ status: string }>('SELECT status FROM ops.operations WHERE id = $1', [id]);
          if ((TERMINAL as readonly string[]).includes(st.rows[0]?.status ?? '')) return stop();
          if (++idle % Math.max(1, Math.round(15_000 / config.ssePollMs)) === 0) res.write(': keep-alive\n\n');
        }
      } catch {
        res.write('event: stream.error\ndata: {"message":"progress temporarily unavailable"}\n\n');
      }
      if (!closed) timer = setTimeout(() => void tick(), config.ssePollMs);
    };
    void tick();
  });

  app.get('/api/incidents', async (req, res) => {
    const q = parse(Page.extend({ state: z.enum(['open', 'resolved']).optional(), severity: z.enum(['info', 'warning', 'critical']).optional() }), req.query);
    res.json(
      await keysetPage<{ id: string; opened_at: string }>(pool, {
        select: `id, fingerprint, severity, state, source, description, affected_resources, ${ts('opened_at')} AS opened_at, ${ts('resolved_at')} AS resolved_at`,
        from: 'ops.incidents',
        tsCol: 'opened_at',
        where: ['($1::text IS NULL OR state = $1)', '($2::text IS NULL OR severity = $2)'],
        params: [q.state ?? null, q.severity ?? null],
        limit: q.limit,
        cursor: q.cursor,
        sortKey: (r) => r.opened_at,
      }),
    );
  });

  app.get('/api/backups', async (req, res) => {
    const q = parse(Page, req.query);
    const policy = await activePolicy(pool);
    res.json(
      await keysetPage<{ id: string; capture_started_at: string }>(pool, {
        // artifact_path is internal (a management filesystem path) and is not exposed.
        select: `id, 'app' AS schema, ${ts('capture_started_at')} AS capture_started_at, ${ts('capture_finished_at')} AS capture_finished_at,
                 bytes::float8 AS bytes, restored_bytes::float8 AS restored_bytes, checksum, capture_status, verification_status,
                 ${ts("(capture_finished_at + make_interval(days => $1::int))")} AS retention_until, ${ts('expired_at')} AS expired_at,
                 ${ts('verified_at')} AS verified_at, is_safety_backup, error_code, error_message`,
        from: 'ops.backups',
        tsCol: 'capture_started_at',
        where: ['$1::int IS NOT NULL'],
        params: [policy.configuration.backup.retention_days],
        limit: q.limit,
        cursor: q.cursor,
        sortKey: (r) => r.capture_started_at,
      }),
    );
  });

  app.get('/api/tests', async (req, res) => {
    const q = parse(Page, req.query);
    res.json(
      await keysetPage<{ id: string; created_at: string }>(pool, {
        select: `id, operation_id, scenario, rate_rps, duration_seconds, status, ${ts('started_at')} AS started_at,
                 ${ts('finished_at')} AS finished_at, summary, ${ts('created_at')} AS created_at`,
        from: 'ops.test_runs',
        tsCol: 'created_at',
        where: ['true'],
        params: [],
        limit: q.limit,
        cursor: q.cursor,
        sortKey: (r) => r.created_at,
      }),
    );
  });

  // ---------- Writes (operator password; every one becomes a queued operation) ----------

  async function accept(req: Request, res: Response, args: Omit<AcceptArgs, 'key' | 'hash'>) {
    const key = IdempotencyKey.safeParse(req.get(IDEMPOTENCY_KEY_HEADER));
    if (!key.success) {
      throw new HttpProblem(400, Code.ValidationFailed, 'Idempotency-Key header is missing or invalid.', {}, [
        { field: IDEMPOTENCY_KEY_HEADER, message: 'must match ^[A-Za-z0-9_-]{8,128}$' },
      ]);
    }
    const out = await acceptOperation(pool, { ...args, key: key.data, hash: requestHash(req.method, req.path, req.body) });
    if (out.replayed) res.setHeader('Idempotent-Replayed', 'true');
    else await recordDispatch(pool, out.operationId, await dispatcher.dispatch(out.operationId, args.kind));
    res.status(202).json({ operation_id: out.operationId, status: 'queued' });
  }

  app.patch('/api/policies', async (req, res) => {
    const pr = PolicyPatch.safeParse(req.body);
    if (!pr.success) {
      // Problems inside `changes` are policy validation failures (422); anything else is malformed (400).
      const inChanges = pr.error.issues.every((i) => i.path[0] === 'changes');
      throw inChanges ? invalid(pr.error, Code.PolicyInvalid, 422) : invalid(pr.error);
    }
    const patch = pr.data;
    await accept(req, res, {
      kind: 'apply_policy',
      routeTemplate: '/api/policies',
      audit: { action: 'policy.change_requested', object: 'policy' },
      prepare: async (c, { operationId }) => {
        const active = await activePolicy(c, true);
        if (patch.expected_version !== active.version) {
          throw new HttpProblem(409, Code.VersionConflict, `expected_version ${patch.expected_version} is stale; the active version is ${active.version}.`);
        }
        const pending = await c.query<{ version: number }>(`SELECT version FROM ops.policy_versions WHERE status = 'pending'`);
        if (pending.rows[0]) {
          throw new HttpProblem(409, Code.PolicyActivationInProgress, `Version ${pending.rows[0].version} is still being activated.`);
        }
        const merged = deepMerge(active.configuration, patch.changes);
        const checked = PolicyConfiguration.safeParse(merged);
        if (!checked.success) throw invalid(checked.error, Code.PolicyInvalid, 422);
        const changed = changedPaths(active.configuration, checked.data);
        if (changed.length === 0) throw new HttpProblem(422, Code.PolicyInvalid, 'The change does not alter the active policy.');
        // Monotonic across failed versions too (docs/05: version is monotonically increasing).
        const max = await c.query<{ v: number }>('SELECT max(version)::int AS v FROM ops.policy_versions');
        const next = (max.rows[0]?.v ?? active.version) + 1;
        await c.query(
          `INSERT INTO ops.policy_versions (version, configuration, status, activation_phase) VALUES ($1, $2, 'pending', 'validating')`,
          [next, JSON.stringify(checked.data)],
        );
        await c.query(
          `INSERT INTO ops.audit_events (source, actor, action, object, old_value, new_value, operation_id)
           VALUES ('dashboard', 'operator', 'policy.version_created', $1, $2, $3, $4)`,
          [`policy:${next}`, JSON.stringify({ version: active.version }), JSON.stringify({ version: next, changed }), operationId],
        );
        return { policy_version: next, from_version: active.version, changed };
      },
    });
  });

  app.post('/api/operations', async (req, res) => {
    const cmd = parse(OperationCommand, req.body);
    await accept(req, res, {
      kind: cmd.kind,
      routeTemplate: '/api/operations',
      audit: { action: 'operation.requested', newValue: cmd },
      prepare: async () => ({ ...(cmd.reason ? { reason: cmd.reason } : {}) }),
    });
  });

  app.post('/api/backups', async (req, res) => {
    await accept(req, res, {
      kind: 'create_backup',
      routeTemplate: '/api/backups',
      audit: { action: 'backup.requested' },
      prepare: async (c, { operationId }) => {
        await assertNoActive(c, ['create_backup'], 'A backup', operationId); // backup.allow_overlap is false
        return {};
      },
    });
  });

  app.post('/api/backups/:id/verify', async (req, res) => {
    const id = req.params['id'] ?? '';
    await accept(req, res, {
      kind: 'verify_backup',
      routeTemplate: '/api/backups/{id}/verify',
      audit: { action: 'backup.verify_requested', object: `backup:${id}` },
      prepare: async (c, { operationId }) => {
        const b = isUuid(id) ? await c.query<{ capture_status: string; expired_at: string | null }>('SELECT capture_status, expired_at FROM ops.backups WHERE id = $1', [id]) : null;
        const row = b?.rows[0];
        if (!row) throw new HttpProblem(404, Code.NotFound, 'Backup not found.');
        if (row.capture_status !== 'captured' || row.expired_at) throw new HttpProblem(422, Code.BackupNotCaptured, 'Only a captured, unexpired backup can be verified.');
        await assertNoActive(c, ['verify_backup', 'restore_application'], 'A verification or restore', operationId);
        return { backup_id: id };
      },
    });
  });

  app.post('/api/restores', async (req, res) => {
    const input = parse(RestoreInput, req.body);
    await accept(req, res, {
      kind: 'restore_application',
      routeTemplate: '/api/restores',
      audit: { action: 'restore.requested', object: `backup:${input.backup_id}` },
      prepare: async (c, { operationId, policy }) => {
        const b = await c.query<{ verification_status: string; age_days: number | null; restored_bytes: string | null; expired_at: string | null }>(
          `SELECT verification_status, extract(epoch FROM now() - verified_at) / 86400 AS age_days, restored_bytes::text, expired_at
           FROM ops.backups WHERE id = $1`,
          [input.backup_id],
        );
        const row = b.rows[0];
        if (!row) throw new HttpProblem(404, Code.NotFound, 'Backup not found.');
        const maxAge = policy.configuration.recovery.max_verification_age_days;
        if (row.verification_status !== 'verified' || row.age_days === null || row.age_days > maxAge || row.expired_at) {
          throw new HttpProblem(422, Code.RestoreNotVerified, `Restore needs a backup verified within the last ${maxAge} days. Verify it first.`);
        }
        if (row.restored_bytes === null) throw new HttpProblem(422, Code.RestoreSizeUnknown, 'The restored size is unknown. Re-verify the backup.');
        const size = await c.query<{ bytes: string }>('SELECT pg_database_size(current_database())::text AS bytes');
        const current = Number(size.rows[0]!.bytes);
        const needed = current + Number(row.restored_bytes) * 1.2;
        if (needed >= RESTORE_HEADROOM_LIMIT_BYTES) {
          throw new HttpProblem(422, Code.RestoreInsufficientHeadroom, `Not enough database space: needs ${(needed / MB).toFixed(0)} MB of the 450 MB limit.`);
        }
        await assertNoActive(c, ['restore_application', 'verify_backup'], 'A restore or verification', operationId);
        return { backup_id: input.backup_id, ...(input.reason ? { reason: input.reason } : {}), headroom_check: { current_bytes: current, restored_bytes: Number(row.restored_bytes) } };
      },
    });
  });

  app.post('/api/tests', async (req, res) => {
    const input = parse(TestRunInput, req.body);
    await accept(req, res, {
      kind: 'start_load_test',
      routeTemplate: '/api/tests',
      audit: { action: 'test.start_requested', newValue: input },
      prepare: async (c, { operationId, policy }) => {
        const d = policy.configuration.demo;
        if (input.rate_rps > d.max_rate_rps || input.duration_seconds > d.max_duration_seconds) {
          throw new HttpProblem(422, Code.TestBoundsExceeded, `Limits are ${d.max_rate_rps} requests/s and ${d.max_duration_seconds} s.`);
        }
        await assertNoActive(c, ['start_load_test'], 'A load test', operationId);
        const running = await c.query(`SELECT 1 FROM ops.test_runs WHERE status <> ALL($1) LIMIT 1`, [TERMINAL]);
        if (running.rowCount) throw new HttpProblem(409, Code.OperationConflict, 'A load test is already queued or running.');
        const t = await c.query<{ id: string }>(
          `INSERT INTO ops.test_runs (operation_id, scenario, rate_rps, duration_seconds) VALUES ($1, $2, $3, $4) RETURNING id`,
          [operationId, input.scenario, input.rate_rps, input.duration_seconds],
        );
        return { test_run_id: t.rows[0]!.id, ...input };
      },
    });
  });

  app.post('/api/tests/:id/stop', async (req, res) => {
    const id = req.params['id'] ?? '';
    await accept(req, res, {
      kind: 'stop_load_test',
      routeTemplate: '/api/tests/{id}/stop',
      audit: { action: 'test.stop_requested', object: `test_run:${id}` },
      prepare: async (c) => {
        const t = isUuid(id) ? await c.query<{ status: string }>('SELECT status FROM ops.test_runs WHERE id = $1 FOR UPDATE', [id]) : null;
        const row = t?.rows[0];
        if (!row) throw new HttpProblem(404, Code.NotFound, 'Test run not found.');
        if ((TERMINAL as readonly string[]).includes(row.status)) throw new HttpProblem(409, Code.TestNotRunning, `The test run already ${row.status}.`);
        return { test_run_id: id };
      },
    });
  });

  // ---------- Dashboard UI ----------
  const indexFile = webDir ? join(webDir, 'index.html') : null;
  if (webDir && indexFile && existsSync(indexFile)) {
    const indexHtml = readFileSync(indexFile);
    const PAGE_CSP =
      "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";
    app.use('/assets', express.static(join(webDir, 'assets'), { index: false, setHeaders: (r) => r.setHeader('Cache-Control', 'public, max-age=31536000, immutable') }));
    app.get('/{*page}', (req, res, next) => {
      if (req.path.startsWith('/api/') || req.path.startsWith('/health/') || req.path.startsWith('/assets/')) return next();
      res.setHeader('Content-Security-Policy', PAGE_CSP);
      res.setHeader('Cache-Control', 'no-cache');
      res.type('html').send(indexHtml);
    });
  }

  app.use((_req, _res, next) => next(new HttpProblem(404, Code.NotFound, 'No such route.')));

  app.use((err: unknown, req: Request, res: Response, _next: NextFunction) => {
    let p: HttpProblem;
    const type = (err as { type?: unknown }).type;
    if (err instanceof HttpProblem) p = err;
    else if (type === 'entity.too.large') p = new HttpProblem(413, Code.PayloadTooLarge, 'Request body exceeds 8 KB.');
    else if (type === 'entity.parse.failed' || type === 'encoding.unsupported' || type === 'charset.unsupported') p = new HttpProblem(400, Code.ValidationFailed, 'Malformed request body.');
    else if ((err as { code?: string }).code === '23505' && req.path === '/api/policies') p = new HttpProblem(409, Code.PolicyActivationInProgress, 'Another policy change was accepted first. Reload and try again.');
    else if (isDependencyError(err)) p = new HttpProblem(503, Code.DependencyUnavailable, 'Database is unavailable.', { 'Retry-After': '5' });
    else {
      console.error(JSON.stringify({ level: 'error', msg: 'unhandled', path: req.path, error: (err as Error)?.name, code: (err as { code?: string })?.code }));
      p = new HttpProblem(500, Code.InternalError, 'Unexpected error.');
    }
    if (res.headersSent) return void res.end();
    sendProblem(res, p);
  });

  return app;
}
