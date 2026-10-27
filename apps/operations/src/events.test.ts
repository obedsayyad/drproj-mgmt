import { readFileSync } from 'node:fs';
import type pg from 'pg';
import { Ajv2020 } from 'ajv/dist/2020.js';
import ajvFormats from 'ajv-formats';
import { describe, expect, it } from 'vitest';
import { recordDispatch, type DispatchResult } from './automation.js';
import { toEventEnvelope } from './events.js';

const schema = JSON.parse(readFileSync(new URL('../../../docs/spec/events.schema.json', import.meta.url), 'utf8'));
const ajv = new Ajv2020({ strict: false });
// ajv-formats is CommonJS; under NodeNext its callable is on `.default`.
(ajvFormats as unknown as typeof ajvFormats.default)(ajv);
const validate = ajv.compile(schema);

/** Runs recordDispatch against a recording client and returns the operation_events row it wrote. */
async function emitted(result: DispatchResult) {
  let params: unknown[] = [];
  const client = {
    query: async (sql: string, p?: unknown[]) => {
      if (sql.includes('INSERT INTO ops.operation_events')) params = p ?? [];
      return { rows: [], rowCount: 1 };
    },
    release: () => undefined,
  };
  await recordDispatch({ connect: async () => client } as unknown as pg.Pool, '5a1f0d2e-3b4c-4d5e-8f60-718293a4b5c6', result);
  const [operation_id, event_type, step, message] = params as [string, string, string, string];
  return { operation_id, event_type, step, message };
}

describe('operation events conform to spec/events.schema.json', () => {
  const results: [string, DispatchResult][] = [
    ['dispatched', { kind: 'dispatched', executionId: 'exec-1' }],
    ['not configured', { kind: 'not_configured' }],
    ['launch failed', { kind: 'failed', reason: 'StackStorm unreachable' }],
  ];

  it.each(results)('dispatch outcome %s', async (_name, result) => {
    const e = await emitted(result);
    const envelope = toEventEnvelope({
      event_id: '0b6f1c1e-9d3a-4a53-9a41-5f3e2d1c0b9a',
      sequence: '7',
      event_type: e.event_type,
      status: 'queued',
      step: e.step,
      message: e.message,
      source: 'dashboard',
      occurred_at: '2026-10-24T10:00:00.000000Z',
      operation_id: e.operation_id,
      policy_version: 1,
    });
    expect(validate(envelope), JSON.stringify(validate.errors)).toBe(true);
  });

  it('keeps the detailed outcome in step while using schema event types', async () => {
    expect(await emitted({ kind: 'dispatched', executionId: 'x' })).toMatchObject({ event_type: 'operation.started', step: 'dispatched' });
    expect(await emitted({ kind: 'not_configured' })).toMatchObject({ event_type: 'operation.waiting', step: 'awaiting_automation' });
    expect(await emitted({ kind: 'failed', reason: 'r' })).toMatchObject({ event_type: 'operation.failed', step: 'dispatch_failed' });
  });

  it('rejects an event type outside the schema enum', () => {
    const bad = toEventEnvelope({
      event_id: '0b6f1c1e-9d3a-4a53-9a41-5f3e2d1c0b9a', sequence: 1, event_type: 'operation.queued', status: null, step: null,
      message: null, source: 'dashboard', occurred_at: '2026-10-24T10:00:00.000000Z', operation_id: '5a1f0d2e-3b4c-4d5e-8f60-718293a4b5c6', policy_version: 1,
    });
    expect(validate(bad)).toBe(false);
  });
});
