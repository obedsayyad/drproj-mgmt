import type pg from 'pg';
import type { Config } from './config.js';

export type DispatchResult =
  | { kind: 'dispatched'; executionId: string }
  | { kind: 'not_configured' }
  | { kind: 'failed'; reason: string };

/** Hands an accepted operation to StackStorm. The API never runs infrastructure work itself. */
export interface Dispatcher {
  dispatch(operationId: string, kind: string): Promise<DispatchResult>;
}

export class StackStormDispatcher implements Dispatcher {
  constructor(private readonly st2: NonNullable<Config['stackstorm']>, private readonly timeoutMs = 5000) {}

  async dispatch(operationId: string, kind: string): Promise<DispatchResult> {
    try {
      const res = await fetch(`${this.st2.url}/v1/executions`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'St2-Api-Key': this.st2.apiKey },
        body: JSON.stringify({ action: `${this.st2.pack}.${kind}`, parameters: { operation_id: operationId } }),
        signal: AbortSignal.timeout(this.timeoutMs),
      });
      if (!res.ok) return { kind: 'failed', reason: `StackStorm returned HTTP ${res.status}` };
      const body = (await res.json().catch(() => null)) as { id?: unknown } | null;
      if (typeof body?.id !== 'string') return { kind: 'failed', reason: 'StackStorm response had no execution id' };
      return { kind: 'dispatched', executionId: body.id };
    } catch (err) {
      return { kind: 'failed', reason: (err as Error).name === 'TimeoutError' ? 'StackStorm timed out' : 'StackStorm unreachable' };
    }
  }
}

export const notConfigured: Dispatcher = { dispatch: async () => ({ kind: 'not_configured' }) };

/**
 * Records the dispatch outcome. Status stays `queued`: only automation moves it to running, so the
 * dashboard never shows work as started because a launch was accepted (docs/06).
 */
export async function recordDispatch(pool: pg.Pool, operationId: string, r: DispatchResult): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    if (r.kind === 'dispatched') {
      await client.query(
        `UPDATE ops.operations SET stackstorm_execution_id = $2, current_step = 'dispatched', updated_at = now()
         WHERE id = $1 AND stackstorm_execution_id IS NULL`,
        [operationId, r.executionId],
      );
    } else {
      await client.query(`UPDATE ops.operations SET current_step = 'awaiting_automation', updated_at = now() WHERE id = $1 AND status = 'queued'`, [operationId]);
    }
    // Event types are the ones in spec/events.schema.json; see events.ts for the mapping.
    const [type, step, message] =
      r.kind === 'dispatched'
        ? ['operation.started', 'dispatched', 'Handed to StackStorm.']
        : r.kind === 'not_configured'
          ? ['operation.waiting', 'awaiting_automation', 'StackStorm is not connected yet. The operation is recorded but has not started.']
          : ['operation.failed', 'dispatch_failed', `${r.reason}. The operation is recorded but has not started; reconciliation can pick it up.`];
    await client.query(
      `INSERT INTO ops.operation_events (operation_id, event_type, status, step, message, source)
       VALUES ($1, $2, 'queued', $3, $4, 'dashboard')`,
      [operationId, type, step, message],
    );
    await client.query('COMMIT');
  } catch {
    await client.query('ROLLBACK').catch(() => undefined);
  } finally {
    client.release();
  }
}
