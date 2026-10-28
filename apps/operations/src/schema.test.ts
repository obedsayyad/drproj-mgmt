import type pg from 'pg';
import { describe, expect, it } from 'vitest';
import { HttpProblem } from './problem.js';
import { assertSchemaRevision } from './schema.js';

const poolWith = (applied: string | null) => ({ query: async () => ({ rows: [{ applied }] }) }) as unknown as pg.Pool;

describe('assertSchemaRevision', () => {
  it('returns applied and expected when the database is at the expected revision', async () => {
    expect(await assertSchemaRevision(poolWith('0015_x.sql'), '0015_x.sql')).toEqual({ applied: '0015_x.sql', expected: '0015_x.sql' });
  });

  it.each([['0014_y.sql'], [null], ['0016_z.sql']])('answers 503 naming both revisions when applied is %s', async (applied) => {
    const err = await assertSchemaRevision(poolWith(applied), '0015_x.sql').catch((e) => e);
    expect(err).toBeInstanceOf(HttpProblem);
    expect(err.status).toBe(503);
    expect(err.message).toContain('0015_x.sql');
    expect(err.message).toContain(applied ?? 'empty');
  });
});
