import type pg from 'pg';
import type { Config } from './config.js';

export type IntegrationState = 'fresh' | 'stale' | 'unavailable';
export interface IntegrationStatus {
  observed_at: string | null;
  state: IntegrationState;
  detail?: string;
}

const now = () => new Date().toISOString();

async function probe(url: string, headers: Record<string, string> = {}): Promise<IntegrationStatus> {
  try {
    const res = await fetch(url, { headers, signal: AbortSignal.timeout(2000) });
    // Drain body to free the socket; content is not used (and never echoed).
    await res.arrayBuffer().catch(() => undefined);
    return res.ok ? { state: 'fresh', observed_at: now() } : { state: 'unavailable', observed_at: now(), detail: `HTTP ${res.status}` };
  } catch (err) {
    return { state: 'unavailable', observed_at: null, detail: (err as Error).name === 'TimeoutError' ? 'timed out' : 'unreachable' };
  }
}

/** No sampler exists yet for this integration; never report it fresh without a timestamped sample. */
const notProbed: IntegrationStatus = { state: 'unavailable', observed_at: null, detail: 'not probed in this phase' };

interface PromTarget {
  scrapePool?: unknown;
  health?: unknown;
  lastScrape?: unknown;
}

/**
 * Freshness of the metric pipeline from Prometheus' own target table (docs/06, docs/10). Fresh only when every
 * expected job has at least one active target, each target is healthy, and each was scraped within
 * stale_after_seconds. A target that is down is stale even though it was just attempted: its series are gone.
 * Read-only; details name jobs and ages, never URLs.
 */
export async function probePrometheus(
  baseUrl: string,
  expectedJobs: string[],
  staleAfterSeconds: number,
  nowMs: number = Date.now(),
  fetchImpl: typeof fetch = fetch,
): Promise<IntegrationStatus> {
  let targets: PromTarget[];
  try {
    const res = await fetchImpl(`${baseUrl}/api/v1/targets?state=active`, { signal: AbortSignal.timeout(2000) });
    if (!res.ok) return { state: 'unavailable', observed_at: null, detail: `HTTP ${res.status}` };
    const body = (await res.json()) as { status?: unknown; data?: { activeTargets?: unknown } };
    if (body.status !== 'success' || !Array.isArray(body.data?.activeTargets)) throw new Error('shape');
    targets = body.data.activeTargets as PromTarget[];
  } catch (err) {
    return { state: 'unavailable', observed_at: null, detail: (err as Error).name === 'TimeoutError' ? 'timed out' : 'unreachable or unreadable response' };
  }
  const problems: string[] = [];
  let oldest: number | null = null;
  for (const job of expectedJobs) {
    const mine = targets.filter((t) => t.scrapePool === job);
    if (mine.length === 0) {
      problems.push(`job ${job} has no active target`);
      continue;
    }
    for (const t of mine) {
      const at = typeof t.lastScrape === 'string' ? Date.parse(t.lastScrape) : NaN;
      if (!Number.isFinite(at) || at <= 0) {
        problems.push(`job ${job} was never scraped`);
        continue;
      }
      oldest = oldest === null ? at : Math.min(oldest, at);
      const age = Math.max(0, Math.round((nowMs - at) / 1000));
      if (t.health !== 'up') problems.push(`job ${job} target is ${typeof t.health === 'string' ? t.health : 'unknown'}`);
      else if (age > staleAfterSeconds) problems.push(`job ${job} last scrape ${age} s ago`);
    }
  }
  const observed_at = oldest === null ? null : new Date(oldest).toISOString();
  if (problems.length === 0) return { state: 'fresh', observed_at };
  return { state: 'stale', observed_at, detail: [...new Set(problems)].slice(0, 5).join('; ') };
}

/** HAProxy health is the freshness of its exporter scrape; without an expected `haproxy` job nothing is claimed. */
export function probeHaproxy(prometheusUrl: string | null, expectedJobs: string[], staleAfterSeconds: number, nowMs: number = Date.now(), fetchImpl: typeof fetch = fetch): Promise<IntegrationStatus> {
  if (!prometheusUrl || !expectedJobs.includes('haproxy')) return Promise.resolve(notProbed);
  return probePrometheus(prometheusUrl, ['haproxy'], staleAfterSeconds, nowMs, fetchImpl);
}

const notConfigured = (what: string): IntegrationStatus => ({ state: 'unavailable', observed_at: null, detail: `${what} is not configured` });

/**
 * Real observations only (docs/11 Phase 2): each tool is probed directly; providers are judged by the
 * last successful reconcile run that StackStorm recorded. Details never include URLs or credentials.
 */
export async function observeIntegrations(cfg: Config, pool: pg.Pool, reconcileIntervalSeconds: number, staleAfterSeconds: number) {
  const supabase = pool
    .query('SELECT 1')
    .then((): IntegrationStatus => ({ state: 'fresh', observed_at: now() }))
    .catch((): IntegrationStatus => ({ state: 'unavailable', observed_at: null, detail: 'query failed' }));
  const stackstorm = cfg.stackstorm ? probe(`${cfg.stackstorm.url}/v1/actions?limit=1`, { 'St2-Api-Key': cfg.stackstorm.apiKey }) : Promise.resolve(notConfigured('StackStorm'));
  // Prometheus readiness alone says nothing about metric freshness, so its target table is judged (P3.2).
  // An HTTP 200 from the HAProxy stats page says nothing about backend freshness, so the exporter scrape is judged instead (P6.1):
  // fresh only while the `haproxy` job's target is up and was scraped recently. Not probed when the job is not expected.
  const prometheus = cfg.prometheusUrl ? probePrometheus(cfg.prometheusUrl, cfg.prometheusExpectedJobs, staleAfterSeconds) : Promise.resolve(notConfigured('Prometheus'));
  const haproxy = probeHaproxy(cfg.prometheusUrl, cfg.prometheusExpectedJobs, staleAfterSeconds);
  const providers = pool
    .query<{ at: string | null; age: number | null; simulated: number }>(
      // Simulation-adapter runs (ops.operations.adapter_mode, docs/10) never make providers look observed.
      `SELECT max(finished_at) FILTER (WHERE adapter_mode IS DISTINCT FROM 'simulation')::text AS at,
              extract(epoch FROM now() - max(finished_at) FILTER (WHERE adapter_mode IS DISTINCT FROM 'simulation'))::float8 AS age,
              count(*) FILTER (WHERE adapter_mode = 'simulation')::int AS simulated
       FROM ops.operations WHERE kind = 'reconcile' AND status = 'succeeded'`,
    )
    .then(({ rows }): IntegrationStatus => {
      const r = rows[0];
      if (!r?.at || r.age === null) {
        return { state: 'unavailable', observed_at: null, detail: r && r.simulated > 0 ? 'only simulated reconcile runs recorded' : 'no successful reconcile run recorded yet' };
      }
      const at = new Date(r.at).toISOString();
      return r.age <= reconcileIntervalSeconds * 3
        ? { state: 'fresh', observed_at: at }
        : { state: 'stale', observed_at: at, detail: `last reconcile ${Math.round(r.age)} s ago` };
    })
    .catch((): IntegrationStatus => ({ state: 'unavailable', observed_at: null, detail: 'query failed' }));
  const [s, st2, prom, hap, prov] = await Promise.all([supabase, stackstorm, prometheus, haproxy, providers]);
  return { supabase: s, stackstorm: st2, prometheus: prom, haproxy: hap, providers: prov };
}
