import express from 'express';
import type pg from 'pg';
import client from 'prom-client';

/** The node_exporter port the fleet exposes (docs/08, operational ports); joins cdr_member_serving to node_cpu_seconds_total. */
const NODE_EXPORTER_PORT = 9100;
export const DB_SIZE_CACHE_MS = 60_000;

/**
 * Read-only gauges from ops tables (docs/10 measurement contract). Real observations only: when a
 * query fails or there is no row, the series is left out instead of reporting 0 or a cached value,
 * so absent()/staleness rules cannot be satisfied by invented data.
 */
export function createOpsMetrics(pool: pg.Pool, now: () => number = Date.now) {
  let sizeCache: { at: number; bytes: number } | null = null;

  async function dbSizeBytes(): Promise<number> {
    if (sizeCache && now() - sizeCache.at < DB_SIZE_CACHE_MS) return sizeCache.bytes;
    const r = await pool.query<{ bytes: string }>('SELECT pg_database_size(current_database())::text AS bytes');
    const bytes = Number(r.rows[0]?.bytes);
    if (!Number.isFinite(bytes)) throw new Error('no size');
    sizeCache = { at: now(), bytes };
    return bytes;
  }

  /** Builds a fresh registry per scrape so a failed read leaves its series out entirely. */
  async function render(): Promise<{ contentType: string; body: string }> {
    const registry = new client.Registry();
    // A prom-client gauge renders as 0 until set, so a gauge is created only once its value is known.
    const gauge = (name: string, help: string, labelNames: string[] = []) => new client.Gauge({ name, help, labelNames, registers: [registry] });
    let ok = 1;
    const attempt = async (fn: () => Promise<void>) => {
      try {
        await fn();
      } catch {
        ok = 0;
      }
    };

    await Promise.all([
      attempt(async () => {
        const r = await pool.query<{ id: string; provider: string; role: string; public_ip: string | null; serving: boolean }>(
          `SELECT id, provider, role, public_ip, (routing_mode = 'active' AND routing_weight > 0) AS serving
           FROM ops.instances WHERE lifecycle_state <> 'deleted'`,
        );
        const serving = gauge('cdr_member_serving', '1 when the instance is routed as active with weight > 0', ['instance_id', 'provider', 'role', 'instance']);
        for (const row of r.rows) {
          serving.set({ instance_id: row.id, provider: row.provider, role: row.role, instance: row.public_ip ? `${row.public_ip}:${NODE_EXPORTER_PORT}` : '' }, row.serving ? 1 : 0);
        }
      }),
      attempt(async () => {
        const bytes = await dbSizeBytes();
        gauge('cdr_supabase_db_size_bytes', 'pg_database_size of the Supabase database (cached 60 s)').set(bytes);
      }),
      attempt(async () => {
        const r = await pool.query<{ t: number | null }>('SELECT extract(epoch FROM max(finished_at))::float8 AS t FROM ops.keepalive_runs WHERE succeeded');
        const t = r.rows[0]?.t;
        if (t != null) gauge('cdr_supabase_keepalive_last_success_timestamp', 'Unix time of the last successful keep-alive').set(t);
      }),
      attempt(async () => {
        const r = await pool.query<{ t: number | null }>(
          `SELECT extract(epoch FROM max(capture_finished_at))::float8 AS t FROM ops.backups
           WHERE capture_status = 'captured' AND NOT is_safety_backup AND expired_at IS NULL`,
        );
        const t = r.rows[0]?.t;
        if (t != null) gauge('cdr_backup_latest_capture_timestamp', 'Unix time the latest captured, unexpired, non-safety backup finished').set(t);
      }),
      attempt(async () => {
        const r = await pool.query<{ version: number }>(`SELECT version FROM ops.policy_versions WHERE status = 'active'`);
        if (r.rows[0]) gauge('cdr_policy_active_version', 'Version number of the active policy').set(r.rows[0].version);
      }),
    ]);

    gauge('cdr_ops_metrics_query_success', '1 when every ops table read for this scrape succeeded').set(ok);
    return { contentType: registry.contentType, body: await registry.metrics() };
  }

  return { render };
}

/** Served on OPS_METRICS_PORT, a separate listener that is not published or routed through Caddy. */
export function createMetricsApp(pool: pg.Pool): express.Express {
  const app = express();
  app.disable('x-powered-by');
  const m = createOpsMetrics(pool);
  app.get('/metrics', async (_req, res) => {
    const { contentType, body } = await m.render();
    res.type(contentType).send(body);
  });
  return app;
}
