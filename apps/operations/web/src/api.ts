export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly errors: { field?: string; message?: string }[] = [],
  ) {
    super(message);
  }
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export type IntegrationState = 'fresh' | 'stale' | 'unavailable';
export interface Overview {
  observed_at: string;
  active_policy_version: number;
  capacity: { desired: Record<string, number>; observed: Record<string, number>; simulated_count: number };
  integrations: Record<string, { state: IntegrationState; observed_at: string | null; detail?: string }>;
  incidents: { open: number; critical: number };
  latest_backup: { id: string; capture_finished_at: string; verification_status: string; age_seconds: number } | null;
}
export interface Operation {
  id: string;
  kind: string;
  status: string;
  policy_version: number;
  current_step: string | null;
  stackstorm_execution_id: string | null;
  parameters: Record<string, unknown>;
  observed_at: string | null;
  created_at: string;
  finished_at: string | null;
  result: Record<string, unknown> | null;
}
/** An events.schema.json envelope; status, step and message travel in `measurements`. */
export interface OpEvent {
  sequence: number;
  event_id: string;
  event_type: string;
  source: string;
  observed_at: string;
  severity?: 'info' | 'warning' | 'critical';
  measurements: { status?: string | null; step?: string | null; message?: string | null };
}
export interface Instance {
  id: string;
  simulated: boolean;
  role: string;
  provider: string;
  slot: string | null;
  vcpu: number | null;
  memory_mb: number | null;
  last_seen_at: string | null;
  routing_weight: number;
  slot_state: string | null;
  lifecycle_state: string;
  health_state: string | null;
  routing_mode: string;
  image_digest: string | null;
}
export interface PolicyVersion {
  version: number;
  status: string;
  activation_phase: string | null;
  configuration: Policy;
  apply_result: Record<string, unknown> | null;
  created_at: string;
  activated_at: string | null;
}
export type Policy = Record<string, Record<string, unknown>> & {
  capacity: { max_cloud_vms_per_provider: { digitalocean: number; linode: number }; warm_provider: string };
  demo: { max_rate_rps: number; max_duration_seconds: number; default_rate_rps: number };
};
export interface Backup {
  id: string;
  capture_started_at: string;
  capture_finished_at: string | null;
  bytes: number | null;
  restored_bytes: number | null;
  capture_status: string;
  verification_status: string;
  retention_until: string | null;
  verified_at: string | null;
  is_safety_backup: boolean;
  error_message: string | null;
}
export interface TestRun {
  id: string;
  scenario: string;
  rate_rps: number;
  duration_seconds: number;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  summary: { p95_ms?: number; error_rate?: number; requests?: number } | null;
}
export interface Incident {
  id: string;
  fingerprint: string;
  severity: string;
  state: string;
  source: string | null;
  description: string | null;
  opened_at: string;
  resolved_at: string | null;
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { ...init, headers: { accept: 'application/json', ...init.headers } });
  } catch {
    throw new ApiError(0, 'NETWORK', 'Could not reach the management API.');
  }
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    const p = (body ?? {}) as { code?: string; detail?: string; title?: string; errors?: ApiError['errors'] };
    throw new ApiError(res.status, p.code ?? 'HTTP_ERROR', p.detail ?? p.title ?? `Request failed (${res.status}).`, p.errors);
  }
  return body as T;
}

export const get = <T>(path: string) => call<T>(path);

/** Every change carries the operator password (checked by Caddy and the API) and a fresh key. */
export function mutate(method: 'POST' | 'PATCH', path: string, password: string, body?: unknown) {
  return call<{ operation_id: string; status: string }>(path, {
    method,
    headers: {
      'content-type': 'application/json',
      'idempotency-key': crypto.randomUUID(),
      authorization: `Basic ${btoa(`operator:${password}`)}`,
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
}
