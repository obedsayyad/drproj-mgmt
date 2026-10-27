// Initializes policy version 1 from docs/config/policy-defaults.json if no policy exists yet.
// Validated with the same schema as dashboard edits. Safe to re-run.
import { readFileSync } from 'node:fs';
import { createPool, inTransaction } from '../src/db.js';
import { PolicyConfiguration } from '@drproj/contracts';

const url = process.env['SUPABASE_OPS_URL'];
if (!url) {
  console.error('SUPABASE_OPS_URL is not set');
  process.exit(1);
}
const file = process.argv[2] ?? new URL('../../../docs/config/policy-defaults.json', import.meta.url);
const parsed = PolicyConfiguration.safeParse(JSON.parse(readFileSync(file, 'utf8')));
if (!parsed.success) {
  console.error(JSON.stringify({ msg: 'policy defaults are invalid', issues: parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`) }));
  process.exit(1);
}
const pool = createPool(url, 1);
try {
  const created = await inTransaction(pool, async (c) => {
    await c.query('LOCK TABLE ops.policy_versions IN EXCLUSIVE MODE');
    const any = await c.query('SELECT 1 FROM ops.policy_versions LIMIT 1');
    if (any.rowCount) return false;
    await c.query(
      `INSERT INTO ops.policy_versions (version, configuration, status, activation_phase, activated_at, apply_result)
       VALUES (1, $1, 'active', 'completed', now(), '{"seeded": true}')`,
      [JSON.stringify(parsed.data)],
    );
    await c.query(`INSERT INTO ops.audit_events (source, action, object, new_value) VALUES ('automation', 'policy.seeded', 'policy:1', '{"version": 1}')`);
    return true;
  });
  console.log(JSON.stringify({ msg: created ? 'policy version 1 seeded' : 'policy already initialized; nothing changed' }));
} finally {
  await pool.end();
}
