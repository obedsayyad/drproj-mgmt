import type pg from 'pg';
import { Code, HttpProblem } from './problem.js';

/** Newest ops migration this build needs (name as in ops.schema_migrations). tests/integration/schema-revision.test.ts fails when it
 *  differs from the newest file in drproj-app, so a new migration cannot ship without bumping it here. */
export const EXPECTED_OPS_SCHEMA_REVISION = '0016_haproxy_command_intent.sql';

export interface SchemaRevision {
  applied: string | null;
  expected: string;
}

/** Throws the 503 dependency problem when the database is not at the revision this build expects (behind or ahead). */
export async function assertSchemaRevision(pool: pg.Pool, expected: string = EXPECTED_OPS_SCHEMA_REVISION): Promise<SchemaRevision> {
  const r = await pool.query<{ applied: string | null }>('SELECT max(name) AS applied FROM ops.schema_migrations');
  const applied = r.rows[0]?.applied ?? null;
  if (applied !== expected) {
    throw new HttpProblem(503, Code.DependencyUnavailable, `Database schema revision is ${applied ?? 'empty'}, this build expects ${expected}. Apply pending migrations.`, { 'Retry-After': '30' });
  }
  return { applied, expected };
}
