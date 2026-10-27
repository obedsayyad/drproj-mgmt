import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { changedPaths, deepMerge, PolicyConfiguration, PolicyConfigurationPatch } from './policy.js';

const defaults = JSON.parse(readFileSync(new URL('../../../docs/config/policy-defaults.json', import.meta.url), 'utf8'));
const issues = (changes: unknown) =>
  PolicyConfiguration.safeParse(deepMerge(defaults, changes)).error?.issues.map((i) => i.path.join('.')) ?? [];

describe('policy validation (docs/03)', () => {
  it('accepts the seed defaults', () => {
    expect(PolicyConfiguration.safeParse(defaults).success).toBe(true);
  });
  it('enforces the cross-field rules', () => {
    expect(issues({ scale_in: { cpu_percent: 60 } })).toEqual(['scale_in.cpu_percent']);
    expect(issues({ provisioning: { readiness_checkpoints_seconds: [240, 200, 480] } })).toEqual(['provisioning.readiness_checkpoints_seconds']);
    expect(issues({ provisioning: { readiness_deadline_seconds: 500 } })).toEqual(['provisioning.readiness_deadline_seconds']);
    expect(issues({ capacity: { warm_provider: 'linode', max_cloud_vms_per_provider: { linode: 0 } } })).toEqual(['capacity.max_cloud_vms_per_provider.linode']);
    expect(issues({ capacity: { max_cloud_vms_per_provider: { linode: 0 } } })).toEqual([]);
    expect(issues({ failback: { primary_traffic_steps_percent: [50, 10, 100] } })).toEqual(['failback.primary_traffic_steps_percent']);
    expect(issues({ failback: { primary_traffic_steps_percent: [10, 50] } })).toEqual(['failback.primary_traffic_steps_percent']);
    expect(issues({ coordination: { lease_renewal_seconds: 50 } })).toEqual(['coordination.lease_renewal_seconds']);
    expect(issues({ health: { timeout_seconds: 10 } })).toEqual(['health.timeout_seconds']);
    expect(issues({ backup: { cron: '0 0 * * *; reboot' } })).toEqual(['backup.cron']);
  });
  it('patches are deep-partial and reject unknown keys', () => {
    expect(PolicyConfigurationPatch.safeParse({ backup: { verify: { timeout_seconds: 600 } } }).success).toBe(true);
    expect(PolicyConfigurationPatch.safeParse({ backup: { path: '/etc' } }).success).toBe(false);
    expect(PolicyConfigurationPatch.safeParse({ secrets: {} }).success).toBe(false);
  });
  it('merges objects, replaces arrays, and reports changed paths', () => {
    const merged = deepMerge(defaults, { failback: { primary_traffic_steps_percent: [25, 100] }, scale_out: { cpu_percent: 70 } });
    expect(merged.failback.primary_traffic_steps_percent).toEqual([25, 100]);
    expect(merged.failback.stable_seconds).toBe(60);
    expect(changedPaths(defaults, merged).sort()).toEqual(['failback.primary_traffic_steps_percent', 'scale_out.cpu_percent']);
  });
});
