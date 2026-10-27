import { createHash } from 'node:crypto';
import type pg from 'pg';
import { inTransaction } from './db.js';
import type { PolicyConfiguration } from '@drproj/contracts';
import { Code, HttpProblem } from './problem.js';

export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, v]) => v !== undefined)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

export const requestHash = (method: string, path: string, body: unknown) =>
  createHash('sha256').update(`${method}\n${path}\n${canonicalJson(body ?? null)}`).digest('hex');

export const TERMINAL = ['succeeded', 'failed', 'cancelled'] as const;

export interface ActivePolicy {
  version: number;
  configuration: PolicyConfiguration;
}

export async function activePolicy(db: pg.Pool | pg.PoolClient, forUpdate = false): Promise<ActivePolicy> {
  const r = await db.query<ActivePolicy>(
    `SELECT version, configuration FROM ops.policy_versions WHERE status = 'active'${forUpdate ? ' FOR UPDATE' : ''}`,
  );
  const row = r.rows[0];
  if (!row) throw new HttpProblem(503, Code.PolicyNotInitialized, 'No active policy. Run the policy seed first.');
  return row;
}

export interface AcceptArgs {
  key: string;
  hash: string;
  routeTemplate: string;
  kind: string;
  /** Domain checks and side records inside the same transaction; returns operation parameters. */
  prepare?: (c: pg.PoolClient, ctx: { operationId: string; policy: ActivePolicy }) => Promise<Record<string, unknown>>;
  audit: { action: string; object?: string; oldValue?: unknown; newValue?: unknown };
}

export interface Accepted {
  operationId: string;
  replayed: boolean;
}

/**
 * Records an operator intent as a queued operation (idempotent on route template + Idempotency-Key, verified by request hash; keys never expire).
 * Validation failures roll back, so the key is not consumed.
 */
export async function acceptOperation(pool: pg.Pool, a: AcceptArgs): Promise<Accepted> {
  return inTransaction(pool, async (c) => {
    const policy = await activePolicy(c);
    await c.query("SET LOCAL lock_timeout = '2s'");
    let inserted: { id: string } | undefined;
    try {
      const r = await c.query<{ id: string }>(
        `INSERT INTO ops.operations (kind, route_template, idempotency_key, request_hash, policy_version, current_step)
         VALUES ($1, $2, $3, $4, $5, 'accepted') ON CONFLICT (route_template, idempotency_key) DO NOTHING RETURNING id`,
        [a.kind, a.routeTemplate, a.key, a.hash, policy.version],
      );
      inserted = r.rows[0];
    } catch (err) {
      if ((err as { code?: string }).code === '55P03') {
        throw new HttpProblem(409, Code.IdempotencyInFlight, 'The original request with this Idempotency-Key is still in progress.', { 'Retry-After': '2' });
      }
      throw err;
    }
    await c.query('RESET lock_timeout');
    if (!inserted) {
      const ex = await c.query<{ id: string; request_hash: string | null }>(
        'SELECT id, request_hash FROM ops.operations WHERE route_template = $1 AND idempotency_key = $2',
        [a.routeTemplate, a.key],
      );
      const row = ex.rows[0];
      if (!row) throw new HttpProblem(409, Code.IdempotencyInFlight, 'Retry shortly.', { 'Retry-After': '2' });
      if (row.request_hash !== a.hash) {
        throw new HttpProblem(422, Code.IdempotencyKeyReused, 'Idempotency-Key was already used with a different request.');
      }
      return { operationId: row.id, replayed: true };
    }
    const operationId = inserted.id;
    const params = a.prepare ? await a.prepare(c, { operationId, policy }) : {};
    await c.query('UPDATE ops.operations SET parameters = $2 WHERE id = $1', [operationId, JSON.stringify(params)]);
    await c.query(
      `INSERT INTO ops.operation_events (operation_id, event_type, status, step, message, source)
       VALUES ($1, 'operation.started', 'queued', 'accepted', 'Requested from the dashboard.', 'dashboard')`,
      [operationId],
    );
    await c.query(
      `INSERT INTO ops.audit_events (source, actor, action, object, old_value, new_value, operation_id)
       VALUES ('dashboard', 'operator', $1, $2, $3, $4, $5)`,
      [
        a.audit.action,
        a.audit.object ?? `operation:${operationId}`,
        a.audit.oldValue === undefined ? null : JSON.stringify(a.audit.oldValue),
        JSON.stringify(a.audit.newValue ?? params),
        operationId,
      ],
    );
    return { operationId, replayed: false };
  });
}

/** Admission check (not control): refuse a second non-terminal operation of these kinds. */
export async function assertNoActive(c: pg.PoolClient, kinds: string[], what: string, selfId: string): Promise<void> {
  // Serialize concurrent admissions of the same kinds.
  await c.query('SELECT pg_advisory_xact_lock(hashtext($1))', [kinds.sort().join(',')]);
  const r = await c.query(
    `SELECT 1 FROM ops.operations WHERE kind = ANY($1) AND status <> ALL($2) AND id <> $3 LIMIT 1`,
    [kinds, TERMINAL, selfId],
  );
  if (r.rowCount) throw new HttpProblem(409, Code.OperationConflict, `${what} is already queued or running.`);
}
