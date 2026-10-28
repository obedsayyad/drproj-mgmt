// The revision the management API expects must be the newest ops migration in drproj-app (repos are checked out side by side).
import { readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { expect, it } from 'vitest';
import { EXPECTED_OPS_SCHEMA_REVISION } from '../../apps/operations/src/schema.js';

const OPS_SQL_DIR = fileURLToPath(new URL('../../../drproj-app/database/migrations/sql/ops', import.meta.url));

it('EXPECTED_OPS_SCHEMA_REVISION is the newest ops migration file', () => {
  const newest = readdirSync(OPS_SQL_DIR).filter((f) => /^\d{4}_.+\.sql$/.test(f)).sort().at(-1);
  expect(EXPECTED_OPS_SCHEMA_REVISION).toBe(newest);
});
