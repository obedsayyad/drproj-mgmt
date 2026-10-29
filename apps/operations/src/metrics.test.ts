import type pg from 'pg';
import { describe, expect, it, vi } from 'vitest';
import { cloudTargets, createOpsMetrics, DB_SIZE_CACHE_MS } from './metrics.js';

type Handler = (sql: string) => { rows: unknown[] } | Error;

function stubPool(handler: Handler) {
  const query = vi.fn(async (sql: string) => {
    const r = handler(sql);
    if (r instanceof Error) throw r;
    return r;
  });
  return { pool: { query } as unknown as pg.Pool, query };
}

const healthy: Handler = (sql) => {
  if (sql.includes('FROM ops.instances')) {
    return {
      rows: [
        { id: 'a', provider: 'self_hosted', role: 'primary', public_ip: '203.0.113.10', serving: true },
        { id: 'b', provider: 'digitalocean', role: 'warm', public_ip: null, serving: false },
      ],
    };
  }
  if (sql.includes('pg_database_size')) return { rows: [{ bytes: '123456' }] };
  if (sql.includes('ops.keepalive_runs')) return { rows: [{ t: 1_700_000_000.5 }] };
  if (sql.includes('ops.backups')) return { rows: [{ t: 1_700_000_100 }] };
  if (sql.includes('ops.policy_versions')) return { rows: [{ version: 4 }] };
  return new Error('unexpected query');
};

describe('ops /metrics', () => {
  it('maps rows to gauges, joining the member to its node_exporter instance', async () => {
    const { body } = await createOpsMetrics(stubPool(healthy).pool).render();
    expect(body).toContain('cdr_member_serving{instance_id="a",provider="self_hosted",role="primary",instance="203.0.113.10:9100"} 1');
    expect(body).toContain('cdr_member_serving{instance_id="b",provider="digitalocean",role="warm",instance=""} 0');
    expect(body).toContain('cdr_supabase_db_size_bytes 123456');
    expect(body).toContain('cdr_supabase_keepalive_last_success_timestamp 1700000000.5');
    expect(body).toContain('cdr_backup_latest_capture_timestamp 1700000100');
    expect(body).toContain('cdr_policy_active_version 4');
    expect(body).toContain('cdr_ops_metrics_query_success 1');
  });

  it('leaves a series out (never 0) when its read fails or has no row, and flags the failure', async () => {
    const { pool } = stubPool((sql) => {
      if (sql.includes('pg_database_size')) return new Error('connection terminated');
      if (sql.includes('ops.keepalive_runs')) return { rows: [{ t: null }] };
      return healthy(sql);
    });
    const { body } = await createOpsMetrics(pool).render();
    const samples = body.split('\n').filter((l) => !l.startsWith('#'));
    expect(samples.some((l) => l.startsWith('cdr_supabase_db_size_bytes'))).toBe(false);
    expect(samples.some((l) => l.startsWith('cdr_supabase_keepalive_last_success_timestamp'))).toBe(false);
    expect(body).toContain('cdr_ops_metrics_query_success 0');
    expect(body).toContain('cdr_policy_active_version 4');
  });

  it('serves the database size from a 60 s cache and refreshes after it, without reusing a value after a failed refresh', async () => {
    let t = 0;
    let fail = false;
    const { pool, query } = stubPool((sql) => (sql.includes('pg_database_size') ? (fail ? new Error('down') : { rows: [{ bytes: '10' }] }) : healthy(sql)));
    const m = createOpsMetrics(pool, () => t);
    const sizeCalls = () => query.mock.calls.filter(([sql]) => String(sql).includes('pg_database_size')).length;
    await m.render();
    t = DB_SIZE_CACHE_MS - 1;
    await m.render();
    expect(sizeCalls()).toBe(1);
    t = DB_SIZE_CACHE_MS;
    fail = true;
    const { body } = await m.render();
    expect(sizeCalls()).toBe(2);
    expect(body.split('\n').some((l) => l.startsWith('cdr_supabase_db_size_bytes'))).toBe(false);
  });
});

describe('cloud service discovery', () => {
  it('lists the real cloud VMs with the scrape port and the role/slot labels of the mgmt role entries', async () => {
    const { pool, query } = stubPool(() => ({ rows: [{ key: 'warm', ip: '203.0.113.5' }, { key: 'elastic-linode-1', ip: '203.0.113.6' }] }));
    expect(await cloudTargets(pool, '9100')).toEqual([
      { targets: ['203.0.113.5:9100'], labels: { role: 'cloud', slot: 'warm' } },
      { targets: ['203.0.113.6:9100'], labels: { role: 'cloud', slot: 'elastic-linode-1' } },
    ]);
    expect(String(query.mock.calls[0]?.[0])).toContain('NOT i.simulated');
  });
});
