import { fileURLToPath } from 'node:url';
import { z } from 'zod';

const optionalUrl = z.preprocess((v) => (v === '' ? undefined : v), z.url().optional());

const Env = z.object({
  SUPABASE_OPS_URL: z.string().min(1),
  OPS_PORT: z.coerce.number().int().min(1).max(65535).default(8080),
  // Internal-only /metrics listener (Compose network); never published or routed through Caddy.
  OPS_METRICS_PORT: z.coerce.number().int().min(1).max(65535).default(9465),
  OPS_DB_POOL_MAX: z.coerce.number().int().default(3),
  // Shared operator password for mutations (docs/06). Required unless OPS_TRUST_PROXY_AUTH=true.
  OPS_OPERATOR_PASSWORD: z.string().min(12).optional(),
  // Explicit opt-in: Caddy basic auth alone guards mutations and the container port is unreachable except via Caddy.
  OPS_TRUST_PROXY_AUTH: z.enum(['true', 'false', '1', '0', '']).transform((v) => v === 'true' || v === '1').default(false),
  ST2_API_URL: optionalUrl,
  ST2_API_KEY: z.string().optional(),
  ST2_PACK: z.string().regex(/^[a-z0-9_]{1,40}$/).default('cdr'),
  PROMETHEUS_URL: optionalUrl,
  // Scrape jobs whose targets must all be fresh for the Prometheus integration to report fresh (docs/06).
  PROMETHEUS_EXPECTED_JOBS: z.string().regex(/^[a-z0-9_-]+(,[a-z0-9_-]+)*$/).default('prometheus,ops,alertmanager'),
  HAPROXY_STATS_URL: optionalUrl,
  OPS_MAX_SSE_STREAMS: z.coerce.number().int().min(1).max(200).default(20),
  OPS_SSE_POLL_MS: z.coerce.number().int().min(100).max(10_000).default(2000),
  OPS_WEB_DIR: z.string().optional(),
});

export interface Config {
  databaseUrl: string;
  port: number;
  metricsPort: number;
  dbPoolMax: number;
  operatorPassword: string | null;
  trustProxyAuth: boolean;
  stackstorm: { url: string; apiKey: string; pack: string } | null;
  prometheusUrl: string | null;
  prometheusExpectedJobs: string[];
  haproxyStatsUrl: string | null;
  maxSseStreams: number;
  ssePollMs: number;
  webDir: string;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const parsed = Env.safeParse(env);
  if (!parsed.success) {
    throw new Error(`invalid or missing configuration: ${parsed.error.issues.map((i) => i.path.join('.')).join(', ')}`);
  }
  const e = parsed.data;
  if (Boolean(e.ST2_API_URL) !== Boolean(e.ST2_API_KEY)) {
    throw new Error('invalid configuration: ST2_API_URL and ST2_API_KEY must be set together');
  }
  if (!e.OPS_OPERATOR_PASSWORD && !e.OPS_TRUST_PROXY_AUTH) {
    throw new Error('invalid configuration: OPS_OPERATOR_PASSWORD is required unless OPS_TRUST_PROXY_AUTH=true');
  }
  if (e.OPS_METRICS_PORT === e.OPS_PORT) {
    throw new Error('invalid configuration: OPS_METRICS_PORT must differ from OPS_PORT');
  }
  if (e.OPS_DB_POOL_MAX < 1 || e.OPS_DB_POOL_MAX > 5) {
    throw new Error('invalid configuration: OPS_DB_POOL_MAX must be an integer from 1 to 5');
  }
  return {
    databaseUrl: e.SUPABASE_OPS_URL,
    port: e.OPS_PORT,
    metricsPort: e.OPS_METRICS_PORT,
    dbPoolMax: e.OPS_DB_POOL_MAX,
    operatorPassword: e.OPS_OPERATOR_PASSWORD ?? null,
    trustProxyAuth: e.OPS_TRUST_PROXY_AUTH,
    stackstorm: e.ST2_API_URL && e.ST2_API_KEY ? { url: e.ST2_API_URL.replace(/\/$/, ''), apiKey: e.ST2_API_KEY, pack: e.ST2_PACK } : null,
    prometheusUrl: e.PROMETHEUS_URL?.replace(/\/$/, '') ?? null,
    prometheusExpectedJobs: e.PROMETHEUS_EXPECTED_JOBS.split(','),
    haproxyStatsUrl: e.HAPROXY_STATS_URL ?? null,
    maxSseStreams: e.OPS_MAX_SSE_STREAMS,
    ssePollMs: e.OPS_SSE_POLL_MS,
    webDir: e.OPS_WEB_DIR ?? fileURLToPath(new URL('../web/dist', import.meta.url)),
  };
}
