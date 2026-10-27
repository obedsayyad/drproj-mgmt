import { z } from 'zod';

// docs/spec/openapi.yaml PolicyConfiguration + docs/03 "Validation". Strict: unknown keys rejected,
// so secrets, paths and commands cannot ride along in a policy.
const int = (min: number, max: number) => z.int().min(min).max(max);
const pct = () => z.number().gt(0).max(100);
const CRON_FIELD = /^[0-9*,/-]{1,20}$/;

export const CloudProvider = z.enum(['digitalocean', 'linode']);

const groups = {
  capacity: z.strictObject({
    max_cloud_vms_per_provider: z.strictObject({ digitalocean: int(0, 5), linode: int(0, 5) }),
    primary_first: z.literal(true),
    warm_provider: CloudProvider,
    warm_instances: z.literal(1),
    elastic_provider_strategy: z.literal('alternate'),
    count_pending_and_uncertain_resources: z.literal(true),
  }),
  scale_out: z.strictObject({
    cpu_percent: pct(),
    sustained_seconds: int(5, 3600),
    cooldown_seconds: int(0, 3600),
    step_vms: z.literal(1),
    activate_warm_standby_first: z.boolean(),
    cpu_aggregation: z.literal('vcpu_weighted_active_fleet'),
  }),
  scale_in: z.strictObject({
    cpu_percent: pct(),
    sustained_seconds: int(30, 7200),
    drain_timeout_seconds: int(5, 600),
    drain_timeout_action: z.literal('pause_and_raise_incident'),
    require_stable_primary: z.literal(true),
    verify_remaining_capacity: z.literal(true),
    protect_warm_slot: z.literal(true),
    step_vms: z.literal(1),
  }),
  health: z.strictObject({
    interval_seconds: int(1, 60),
    consecutive_failures: int(1, 10),
    consecutive_successes: int(1, 10),
    timeout_seconds: int(1, 30),
  }),
  failover: z.strictObject({
    ensure_elastic_instance_after_primary_failure: z.boolean(),
    elastic_instances_after_primary_failure: z.literal(1),
    share_cloud_traffic_when_elastic_ready: z.boolean(),
  }),
  failback: z.strictObject({
    automatic: z.boolean(),
    stable_seconds: int(10, 3600),
    primary_traffic_steps_percent: z.array(int(1, 100)).min(1).max(6),
    step_observation_seconds: int(5, 600),
    final_step_observation_seconds: int(5, 600),
    gate_consecutive_health_successes: int(1, 10),
    gate_max_5xx_ratio: z.number().min(0).max(1),
    gate_max_p95_ratio_to_baseline: z.number().min(1).max(10),
    gate_max_primary_cpu_percent: pct(),
    reverse_on_regression: z.literal(true),
  }),
  provisioning: z.strictObject({
    readiness_checkpoints_seconds: z.array(int(30, 1800)).min(1).max(6),
    readiness_deadline_seconds: int(30, 1800),
    admit_early_when_ready: z.literal(true),
    retries_per_provider: int(0, 3),
    reconcile_before_retry: z.literal(true),
    stop_and_raise_incident_after_exhaustion: z.literal(true),
    hold_slot_on_uncertain_existence: z.literal(true),
  }),
  coordination: z.strictObject({
    capacity_mutations_per_application: z.literal(1),
    reconcile_interval_seconds: int(15, 3600),
    lease_seconds: int(15, 600),
    lease_renewal_seconds: int(5, 300),
    recheck_after_lock: z.literal(true),
  }),
  monitoring: z.strictObject({
    scrape_seconds: int(1, 60),
    evaluation_seconds: int(1, 60),
    cpu_window_seconds: int(10, 300),
    stale_after_seconds: int(10, 600),
    prometheus_retention_days: int(1, 15),
    exclude_idle_standby_and_support_hosts: z.literal(true),
  }),
  alerts: z.strictObject({
    automation_group_wait_seconds: int(0, 60),
    group_interval_seconds: int(1, 600),
    repeat_interval_seconds: int(5, 3600),
    notifications: z.array(z.literal('dashboard')).max(1),
  }),
  backup: z.strictObject({
    mechanism: z.literal('pg_dump_custom'),
    schema: z.literal('app'),
    cron: z
      .string()
      .max(64)
      .refine((c) => {
        const f = c.trim().split(/\s+/);
        return f.length === 5 && f.every((x) => CRON_FIELD.test(x));
      }, 'must be a 5-field cron expression (digits, * , / - only)'),
    timezone: z.literal('UTC'),
    retention_days: int(1, 365),
    storage: z.literal('management_persistent_volume'),
    allow_overlap: z.literal(false),
    retries: int(0, 3),
    retry_delay_seconds: int(10, 3600),
    preserve_active_recovery_artifacts: z.literal(true),
    verify: z.strictObject({ enabled: z.boolean(), after_capture: z.boolean(), timeout_seconds: int(60, 3600) }),
  }),
  recovery: z.strictObject({
    operator_started_only: z.literal(true),
    require_verified_backup: z.literal(true),
    max_verification_age_days: int(1, 30),
    verify_isolated_restore_first: z.literal(true),
    global_app_maintenance: z.literal(true),
    safety_dump_before_live_restore: z.literal(true),
    restore_schema: z.literal('app'),
    preserve_schema: z.literal('ops'),
    rollback_attempts: int(0, 1),
    keep_maintenance_on_unverified_failure: z.literal(true),
  }),
  demo: z.strictObject({
    default_rate_rps: int(1, 200),
    max_rate_rps: int(1, 200),
    max_duration_seconds: int(1, 600),
    default_work_ms: int(1, 250),
    max_work_ms: int(1, 250),
    max_cpu_workers_per_instance: z.literal(1),
    max_queued_work_items: int(1, 256),
  }),
  runtime: z.strictObject({
    app_db_pool_max: int(1, 5),
    api_idempotency_retention_hours: int(1, 168),
    custom_login: z.literal(false),
    firewall_automation: z.literal(false),
    vpn_required: z.literal(false),
    supabase_keepalive_interval_hours: int(1, 24),
  }),
  maintenance: z.strictObject({ retry_after_seconds: int(1, 3600) }),
};

const increasing = (xs: number[]) => xs.every((x, i) => i === 0 || x > xs[i - 1]!);

export const PolicyConfiguration = z
  .strictObject({ schema_version: z.literal(1), ...groups })
  .superRefine((p, ctx) => {
    const fail = (path: (string | number)[], message: string) => ctx.addIssue({ code: 'custom', path, message });
    if (p.scale_in.cpu_percent >= p.scale_out.cpu_percent) {
      fail(['scale_in', 'cpu_percent'], 'must be lower than scale_out.cpu_percent');
    }
    const cps = p.provisioning.readiness_checkpoints_seconds;
    if (!increasing(cps)) fail(['provisioning', 'readiness_checkpoints_seconds'], 'must strictly increase');
    if (cps.at(-1) !== p.provisioning.readiness_deadline_seconds) {
      fail(['provisioning', 'readiness_deadline_seconds'], 'must equal the last readiness checkpoint');
    }
    if (p.capacity.max_cloud_vms_per_provider[p.capacity.warm_provider] < 1) {
      fail(['capacity', 'max_cloud_vms_per_provider', p.capacity.warm_provider], 'warm provider needs a cap of at least 1');
    }
    const steps = p.failback.primary_traffic_steps_percent;
    if (!increasing(steps) || steps.at(-1) !== 100) {
      fail(['failback', 'primary_traffic_steps_percent'], 'must strictly increase and end at 100');
    }
    if (p.health.timeout_seconds >= p.health.interval_seconds) {
      fail(['health', 'timeout_seconds'], 'must be less than health.interval_seconds');
    }
    if (p.coordination.lease_renewal_seconds * 3 > p.coordination.lease_seconds) {
      fail(['coordination', 'lease_renewal_seconds'], 'must be at most one third of lease_seconds');
    }
    if (p.demo.default_rate_rps > p.demo.max_rate_rps) fail(['demo', 'default_rate_rps'], 'must not exceed max_rate_rps');
    if (p.demo.default_work_ms > p.demo.max_work_ms) fail(['demo', 'default_work_ms'], 'must not exceed max_work_ms');
  });
export type PolicyConfiguration = z.infer<typeof PolicyConfiguration>;

// Deep-partial patch: each group optional and partial; nested objects partial; arrays replace whole.
const partialGroups = Object.fromEntries(
  Object.entries(groups).map(([k, g]) => {
    const shape = Object.fromEntries(
      Object.entries(g.shape).map(([f, s]) => [
        f,
        (s instanceof z.ZodObject ? (s as z.ZodObject).partial() : (s as z.ZodType)).optional(),
      ]),
    );
    return [k, z.strictObject(shape).optional()];
  }),
);
// The shape is built from `groups` at runtime, which erases its static type; state it explicitly.
// contracts/src/drift.ts checks this type against the generated openapi types.
type DeepPartial<T> = T extends readonly unknown[] ? T : T extends object ? { [K in keyof T]?: DeepPartial<T[K]> } : T;
export type PolicyConfigurationPatch = DeepPartial<PolicyConfiguration>;
export const PolicyConfigurationPatch = z.strictObject({
  schema_version: z.literal(1).optional(),
  ...partialGroups,
}) as unknown as z.ZodType<PolicyConfigurationPatch>;

export const PolicyPatch = z.strictObject({
  expected_version: z.int().min(1),
  changes: PolicyConfigurationPatch,
});

type Json = null | boolean | number | string | Json[] | { [k: string]: Json };
const isObj = (v: unknown): v is Record<string, Json> => typeof v === 'object' && v !== null && !Array.isArray(v);

/** Objects merge key by key; arrays and scalars replace. */
export function deepMerge<T>(base: T, patch: unknown): T {
  if (!isObj(base) || !isObj(patch)) return (patch === undefined ? base : patch) as T;
  const out: Record<string, Json> = { ...base };
  for (const [k, v] of Object.entries(patch)) if (v !== undefined) out[k] = deepMerge(out[k], v) as Json;
  return out as T;
}

/** Paths whose value differs; used for the audit record and the dashboard diff. */
export function changedPaths(a: unknown, b: unknown, prefix = ''): string[] {
  if (isObj(a) && isObj(b)) {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    return [...keys].flatMap((k) => changedPaths(a[k], b[k], prefix ? `${prefix}.${k}` : k));
  }
  return JSON.stringify(a) === JSON.stringify(b) ? [] : [prefix];
}
