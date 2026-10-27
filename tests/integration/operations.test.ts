// REAL local Postgres (TEST_DATABASE_URL), not Supabase. The API connects as a login role that is
// only a member of dr_ops. StackStorm is a local HTTP test double (labelled; not the real service).
// Covers T11 (policy edit), T24 (k6 bounds), T31 (operator auth) and the Phase 2 gate.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { Ajv2020 } from 'ajv/dist/2020.js';
import ajvFormats from 'ajv-formats';
import pg from 'pg';
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { runMigrations } from '@drproj/migrations';
import { createApp, loadConfig, notConfigured, StackStormDispatcher, type Dispatcher } from '@drproj/operations';

// Repos are checked out side by side: SQL is read straight from ../drproj-app, never from a copied install.
const MIGRATIONS_SQL_DIR = fileURLToPath(new URL('../../../drproj-app/database/migrations/sql', import.meta.url));
const url = process.env['TEST_DATABASE_URL'];
const OPS_USER = 'ops_api_test';
const OPS_PW = 'test-only-ops-password';
const OPERATOR_PW = 'operator-test-password';
const ST2_KEY = 'st2-test-key-should-never-leak';
const defaults = JSON.parse(readFileSync(new URL('../../docs/config/policy-defaults.json', import.meta.url), 'utf8'));
const eventAjv = new Ajv2020({ strict: false });
(ajvFormats as unknown as typeof ajvFormats.default)(eventAjv);
const validateEvent = eventAjv.compile(JSON.parse(readFileSync(new URL('../../docs/spec/events.schema.json', import.meta.url), 'utf8')));
const auth = { authorization: `Basic ${Buffer.from(`operator:${OPERATOR_PW}`).toString('base64')}` };

/** Test double for the StackStorm executions API. */
function fakeStackStorm() {
  const calls: { action: string; parameters: Record<string, unknown>; key: string | undefined }[] = [];
  let mode: 'ok' | 'error' = 'ok';
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      if (mode === 'error') return void res.writeHead(500).end();
      if (req.method === 'POST' && req.url === '/v1/executions') {
        const b = JSON.parse(body);
        calls.push({ ...b, key: req.headers['st2-api-key'] as string | undefined });
        return void res.writeHead(201, { 'content-type': 'application/json' }).end(JSON.stringify({ id: `exec-${calls.length}` }));
      }
      res.writeHead(200, { 'content-type': 'application/json' }).end('[]');
    });
  });
  return { server, calls, setMode: (m: 'ok' | 'error') => (mode = m) };
}

async function listen(s: http.Server) {
  await new Promise<void>((r) => s.listen(0, '127.0.0.1', r));
  return `http://127.0.0.1:${(s.address() as AddressInfo).port}`;
}

describe.skipIf(!url)('operations API (real Postgres)', () => {
  let admin: pg.Client;
  let pool: pg.Pool;
  let base: string;
  let server: http.Server;
  const st2 = fakeStackStorm();
  let keySeq = 0;
  const key = () => `ops-key-${Date.now()}-${++keySeq}`;
  const q = (sql: string, p?: unknown[]) => admin.query(sql, p);

  const send = (method: string, path: string, body?: unknown, headers: Record<string, string> = auth, k: string | null = key()) =>
    fetch(`${base}${path}`, {
      method,
      headers: { 'content-type': 'application/json', ...(k ? { 'idempotency-key': k } : {}), ...headers },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });

  async function startWith(dispatcher: Dispatcher) {
    const u = new URL(url!);
    u.username = OPS_USER;
    u.password = OPS_PW;
    const config = loadConfig({ SUPABASE_OPS_URL: u.toString(), OPS_OPERATOR_PASSWORD: OPERATOR_PW, OPS_SSE_POLL_MS: '100', ST2_API_URL: st2Url, ST2_API_KEY: ST2_KEY });
    pool = new pg.Pool({ connectionString: config.databaseUrl, max: 4 });
    server = http.createServer(createApp({ config, pool, dispatcher }));
    base = await listen(server);
    return config;
  }
  let st2Url = '';

  beforeAll(async () => {
    admin = new pg.Client({ connectionString: url });
    await admin.connect();
    await q('DROP SCHEMA IF EXISTS app CASCADE');
    await q('DROP SCHEMA IF EXISTS ops CASCADE');
    await runMigrations({ connectionString: url!, sqlDir: MIGRATIONS_SQL_DIR });
    await q(`DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${OPS_USER}') THEN CREATE ROLE ${OPS_USER} LOGIN; END IF; END $$`);
    await q(`ALTER ROLE ${OPS_USER} PASSWORD '${OPS_PW}'`);
    await q(`GRANT dr_ops TO ${OPS_USER}`);
    st2Url = await listen(st2.server);
    const cfg = loadConfig({ SUPABASE_OPS_URL: 'postgres://x@127.0.0.1/x', OPS_OPERATOR_PASSWORD: OPERATOR_PW, ST2_API_URL: st2Url, ST2_API_KEY: ST2_KEY });
    await startWith(new StackStormDispatcher(cfg.stackstorm!));
  });

  afterAll(async () => {
    await new Promise((r) => server?.close(r));
    await new Promise((r) => st2.server.close(r));
    await pool?.end();
    await admin?.end();
  });

  beforeEach(async () => {
    st2.setMode('ok');
    await q(`TRUNCATE ops.operation_events, ops.audit_events, ops.recovery_runs, ops.test_runs, ops.processed_events, ops.incident_events, ops.incidents, ops.backups, ops.operation_attempts, ops.operations, ops.policy_versions CASCADE`);
    await q(`INSERT INTO ops.policy_versions (version, configuration, status, activation_phase, activated_at) VALUES (1, $1, 'active', 'completed', now())`, [defaults]);
  });

  it('T31: mutations need the operator password; reads stay open; audit actor is operator', async () => {
    const none = await send('POST', '/api/operations', { kind: 'reconcile' }, {});
    expect(none.status).toBe(401);
    expect(none.headers.get('www-authenticate')).toMatch(/^Basic/);
    const wrong = await send('POST', '/api/operations', { kind: 'reconcile' }, { authorization: `Basic ${Buffer.from('operator:nope').toString('base64')}` });
    expect(wrong.status).toBe(401);
    for (const m of ['PUT', 'DELETE']) expect((await send(m, '/api/operations', {}, {})).status).toBe(401);
    expect((await fetch(`${base}/api/overview`)).status).toBe(200);
    const ok = await send('POST', '/api/operations', { kind: 'reconcile' });
    expect(ok.status).toBe(202);
    const audit = await q(`SELECT source, actor, action FROM ops.audit_events WHERE action = 'operation.requested'`);
    expect(audit.rows).toEqual([{ source: 'dashboard', actor: 'operator', action: 'operation.requested' }]);
  });

  it('operations are recorded, handed to StackStorm and never reported as started by the API', async () => {
    const r = await send('POST', '/api/operations', { kind: 'scale_out', reason: 'demo' });
    expect(r.status).toBe(202);
    const { operation_id, status } = (await r.json()) as { operation_id: string; status: string };
    expect(status).toBe('queued');
    const call = st2.calls.at(-1)!;
    expect(call).toMatchObject({ action: 'cdr.scale_out', parameters: { operation_id }, key: ST2_KEY });
    const op = (await (await fetch(`${base}/api/operations/${operation_id}`)).json()) as Record<string, unknown>;
    expect(op).toMatchObject({ kind: 'scale_out', status: 'queued', policy_version: 1, current_step: 'dispatched', parameters: { reason: 'demo' } });
    expect(op['stackstorm_execution_id']).toMatch(/^exec-/);

    st2.setMode('error');
    const failed = (await (await send('POST', '/api/operations', { kind: 'reconcile' })).json()) as { operation_id: string };
    const ev = await q('SELECT event_type FROM ops.operation_events WHERE operation_id = $1 ORDER BY sequence', [failed.operation_id]);
    expect(ev.rows.map((e) => e.event_type)).toEqual(['operation.started', 'operation.failed']);
    const steps = await q('SELECT step FROM ops.operation_events WHERE operation_id = $1 ORDER BY sequence', [failed.operation_id]);
    expect(steps.rows.map((e) => e.step)).toEqual(['accepted', 'dispatch_failed']);
    expect((await q('SELECT status FROM ops.operations WHERE id = $1', [failed.operation_id])).rows[0].status).toBe('queued');

    expect((await send('POST', '/api/operations', { kind: 'restore_application' })).status).toBe(400);
    expect((await send('POST', '/api/operations', { kind: 'reconcile', shell: 'rm -rf /' })).status).toBe(400);
  });

  it('without StackStorm, operations wait visibly instead of pretending to run', async () => {
    const own = http.createServer(createApp({ config: loadConfig({ SUPABASE_OPS_URL: pool.options.connectionString!, OPS_OPERATOR_PASSWORD: OPERATOR_PW }), pool, dispatcher: notConfigured }));
    const ownBase = await listen(own);
    try {
      const r = await fetch(`${ownBase}/api/operations`, { method: 'POST', headers: { 'content-type': 'application/json', 'idempotency-key': key(), ...auth }, body: '{"kind":"reconcile"}' });
      const { operation_id } = (await r.json()) as { operation_id: string };
      const op = (await (await fetch(`${ownBase}/api/operations/${operation_id}`)).json()) as Record<string, unknown>;
      expect(op).toMatchObject({ status: 'queued', current_step: 'awaiting_automation', stackstorm_execution_id: null });
      const ov = (await (await fetch(`${ownBase}/api/overview`)).json()) as { integrations: Record<string, { state: string; detail?: string }> };
      expect(ov.integrations['stackstorm']).toMatchObject({ state: 'unavailable', detail: 'StackStorm is not configured' });
    } finally {
      await new Promise((r) => own.close(r));
    }
  });

  it('progress stream replays durable events and resumes after Last-Event-ID', async () => {
    const { operation_id } = (await (await send('POST', '/api/operations', { kind: 'reconcile' })).json()) as { operation_id: string };
    const read = async (headers: Record<string, string> = {}) => {
      const ctrl = new AbortController();
      const res = await fetch(`${base}/api/operations/${operation_id}/events`, { headers, signal: ctrl.signal });
      expect(res.headers.get('content-type')).toContain('text/event-stream');
      const reader = res.body!.getReader();
      let text = '';
      const deadline = Date.now() + 3000;
      while (Date.now() < deadline) {
        const { value, done } = await reader.read();
        if (done) break;
        text += new TextDecoder().decode(value);
        if (text.includes('operation.succeeded')) break;
      }
      ctrl.abort();
      return [...text.matchAll(/^id: (\d+)\ndata: (.*)$/gm)].map((m) => {
        const envelope = JSON.parse(m[2]!) as { event_type: string; sequence: number };
        // Every streamed event must be a valid spec/events.schema.json document.
        expect(validateEvent(envelope), JSON.stringify(validateEvent.errors)).toBe(true);
        expect(envelope.sequence).toBe(Number(m[1]));
        return { id: Number(m[1]), type: envelope.event_type };
      });
    };
    // Automation (simulated here by SQL, as the StackStorm action will do) finishes the operation.
    setTimeout(() => {
      void q(`INSERT INTO ops.operation_events (operation_id, event_type, status, source) VALUES ($1, 'operation.succeeded', 'succeeded', 'automation')`, [operation_id]).then(() =>
        q(`UPDATE ops.operations SET status = 'succeeded', finished_at = now() WHERE id = $1`, [operation_id]),
      );
    }, 300);
    const all = await read();
    expect(all.map((e) => e.type)).toEqual(['operation.started', 'operation.started', 'operation.succeeded']);
    const resumed = await read({ 'last-event-id': String(all[0]!.id) });
    expect(resumed.map((e) => e.type)).toEqual(['operation.started', 'operation.succeeded']);
    expect((await fetch(`${base}/api/operations/00000000-0000-4000-8000-000000000000/events`)).status).toBe(404);
  });

  it('T11: policy edits validate, detect version conflicts, keep the old version on failed activation', async () => {
    const stale = await send('PATCH', '/api/policies', { expected_version: 7, changes: { scale_out: { cpu_percent: 70 } } });
    expect(stale.status).toBe(409);
    expect(await stale.json()).toMatchObject({ code: 'VERSION_CONFLICT' });

    const invalid = await send('PATCH', '/api/policies', { expected_version: 1, changes: { scale_in: { cpu_percent: 65 }, capacity: { max_cloud_vms_per_provider: { digitalocean: 0 } } } });
    expect(invalid.status).toBe(422);
    const body = (await invalid.json()) as { code: string; errors: { field: string }[] };
    expect(body.code).toBe('POLICY_INVALID');
    expect(body.errors.map((e) => e.field).sort()).toEqual(['capacity.max_cloud_vms_per_provider.digitalocean', 'scale_in.cpu_percent']);
    expect((await send('PATCH', '/api/policies', { expected_version: 1, changes: { capacity: { max_cloud_vms_per_provider: { linode: 6 } } } })).status).toBe(422);
    expect((await send('PATCH', '/api/policies', { expected_version: 1, changes: { runtime: { db_password: 'x' } } })).status).toBe(422);
    expect((await send('PATCH', '/api/policies', { changes: {} })).status).toBe(400);
    expect((await send('PATCH', '/api/policies', { expected_version: 1, changes: { backup: { cron: '0 0 * * * ; rm' } } })).status).toBe(422);
    expect(Number((await q('SELECT count(*) FROM ops.operations')).rows[0].count)).toBe(0);

    const k = key();
    const change = { expected_version: 1, changes: { scale_out: { cpu_percent: 70 } } };
    const ok = await send('PATCH', '/api/policies', change, auth, k);
    expect(ok.status).toBe(202);
    const accepted = (await ok.json()) as { operation_id: string };
    const replay = await send('PATCH', '/api/policies', change, auth, k);
    expect(replay.status).toBe(202);
    expect(replay.headers.get('idempotent-replayed')).toBe('true');
    expect(await replay.json()).toEqual({ ...accepted, status: 'queued' });
    expect((await send('PATCH', '/api/policies', { ...change, changes: { scale_out: { cpu_percent: 71 } } }, auth, k)).status).toBe(422);
    expect(st2.calls.at(-1)).toMatchObject({ action: 'cdr.apply_policy', parameters: { operation_id: accepted.operation_id } });

    const busy = await send('PATCH', '/api/policies', { expected_version: 1, changes: { scale_out: { cooldown_seconds: 90 } } });
    expect(busy.status).toBe(409);
    expect(await busy.json()).toMatchObject({ code: 'POLICY_ACTIVATION_IN_PROGRESS' });

    // apply_policy workflow reports failure: v1 stays active.
    expect((await q(`SELECT ops.finish_policy_activation(2, false, '{"error":"haproxy reload failed"}') AS s`)).rows[0].s).toBe('failed');
    let list = (await (await fetch(`${base}/api/policies`)).json()) as { items: { version: number; status: string }[] };
    expect(list.items.map((i) => [i.version, i.status])).toEqual([[2, 'failed'], [1, 'active']]);

    // Retry and succeed: v3 active, v1 superseded.
    expect((await send('PATCH', '/api/policies', change)).status).toBe(202);
    expect((await q(`SELECT ops.finish_policy_activation(3, true, '{"applied":["prometheus"]}') AS s`)).rows[0].s).toBe('active');
    list = (await (await fetch(`${base}/api/policies`)).json()) as typeof list;
    expect(list.items.map((i) => [i.version, i.status])).toEqual([[3, 'active'], [2, 'failed'], [1, 'superseded']]);
    await expect(q(`SELECT ops.finish_policy_activation(3, true, '{}')`)).rejects.toThrow(/not pending/);
  });

  it('T11: concurrent edits against the same version accept exactly one', async () => {
    const results = await Promise.all(
      [61, 62, 63, 64].map((cpu) => send('PATCH', '/api/policies', { expected_version: 1, changes: { scale_out: { cpu_percent: cpu } } })),
    );
    const codes = results.map((r) => r.status).sort();
    expect(codes).toEqual([202, 409, 409, 409]);
    expect(Number((await q(`SELECT count(*) FROM ops.policy_versions WHERE status = 'pending'`)).rows[0].count)).toBe(1);
  });

  it('T24: load tests are bounded by the contract and by the active policy; manual stop works', async () => {
    expect((await send('POST', '/api/tests', { scenario: 'baseline', rate_rps: 201, duration_seconds: 60 })).status).toBe(400);
    expect((await send('POST', '/api/tests', { scenario: 'baseline', rate_rps: 10, duration_seconds: 601 })).status).toBe(400);
    expect((await send('POST', '/api/tests', { scenario: 'other', rate_rps: 10, duration_seconds: 60 })).status).toBe(400);
    await q(`UPDATE ops.policy_versions SET configuration = jsonb_set(configuration, '{demo,max_rate_rps}', '50') WHERE status = 'active'`);
    const over = await send('POST', '/api/tests', { scenario: 'baseline', rate_rps: 100, duration_seconds: 60 });
    expect(over.status).toBe(422);
    expect(await over.json()).toMatchObject({ code: 'TEST_BOUNDS_EXCEEDED' });

    const ok = await send('POST', '/api/tests', { scenario: 'baseline', rate_rps: 50, duration_seconds: 600 });
    expect(ok.status).toBe(202);
    expect((await send('POST', '/api/tests', { scenario: 'smoke', rate_rps: 1, duration_seconds: 10 })).status).toBe(409);
    const runs = (await (await fetch(`${base}/api/tests`)).json()) as { items: { id: string; status: string; rate_rps: number }[] };
    expect(runs.items).toHaveLength(1);
    expect(runs.items[0]).toMatchObject({ status: 'queued', rate_rps: 50 });
    const id = runs.items[0]!.id;
    expect((await send('POST', `/api/tests/${id}/stop`)).status).toBe(202);
    expect((await send('POST', '/api/tests/00000000-0000-4000-8000-000000000000/stop')).status).toBe(404);
    await q(`UPDATE ops.test_runs SET status = 'cancelled' WHERE id = $1`, [id]);
    expect((await send('POST', `/api/tests/${id}/stop`)).status).toBe(409);
  });

  it('restore admission: verified within 7 days, known size, enough headroom, one at a time', async () => {
    const ins = (cols: string) => q(`INSERT INTO ops.backups (capture_status, verification_status, capture_finished_at ${cols ? ', ' + cols.split('|')[0] : ''}) VALUES ('captured', ${cols.split('|')[1] ?? "'unverified'"}, now()${cols ? ', ' + cols.split('|')[2] : ''}) RETURNING id`);
    const restore = async (id: string) => send('POST', '/api/restores', { backup_id: id });
    const unverified = (await q(`INSERT INTO ops.backups (capture_status, capture_finished_at) VALUES ('captured', now()) RETURNING id`)).rows[0].id;
    const old = (await q(`INSERT INTO ops.backups (capture_status, verification_status, verified_at, restored_bytes) VALUES ('captured', 'verified', now() - interval '8 days', 1000) RETURNING id`)).rows[0].id;
    const sizeless = (await q(`INSERT INTO ops.backups (capture_status, verification_status, verified_at) VALUES ('captured', 'verified', now()) RETURNING id`)).rows[0].id;
    const huge = (await q(`INSERT INTO ops.backups (capture_status, verification_status, verified_at, restored_bytes) VALUES ('captured', 'verified', now(), 400 * 1024 * 1024) RETURNING id`)).rows[0].id;
    const good = (await q(`INSERT INTO ops.backups (capture_status, verification_status, verified_at, restored_bytes) VALUES ('captured', 'verified', now(), 1000000) RETURNING id`)).rows[0].id;
    void ins;
    for (const [id, code] of [[unverified, 'RESTORE_NOT_VERIFIED'], [old, 'RESTORE_NOT_VERIFIED'], [sizeless, 'RESTORE_SIZE_UNKNOWN'], [huge, 'RESTORE_INSUFFICIENT_HEADROOM']] as const) {
      const r = await restore(id);
      expect(r.status).toBe(422);
      expect(await r.json()).toMatchObject({ code });
    }
    expect((await restore('00000000-0000-4000-8000-000000000000')).status).toBe(404);
    expect((await restore(good)).status).toBe(202);
    expect((await restore(good)).status).toBe(409);
    expect((await send('POST', `/api/backups/${good}/verify`)).status).toBe(409);

    const capturing = (await q(`INSERT INTO ops.backups DEFAULT VALUES RETURNING id`)).rows[0].id;
    expect((await send('POST', `/api/backups/${capturing}/verify`)).status).toBe(422);
    expect((await send('POST', '/api/backups')).status).toBe(202);
    expect((await send('POST', '/api/backups')).status).toBe(409);
  });

  it('public reads redact instance addresses and never echo secrets; integrations report real state', async () => {
    // The 13 slot rows are seeded by ops/0009; occupy 'warm' the way the control functions would.
    const warmId = (await q(`INSERT INTO ops.instances (slot_key, provider, provider_resource_id, role, public_ip, vcpu, memory_mb, lifecycle_state, routing_mode, slot_state)
             VALUES ('warm', 'digitalocean', 'droplet-123456', 'warm', '198.51.100.23', 1, 1024, 'ready', 'backup', 'bound') RETURNING id`)).rows[0].id;
    await q(`UPDATE ops.capacity_slots SET state = 'bound', bound_instance_id = $1 WHERE key = 'warm'`, [warmId]);
    const inst = (await (await fetch(`${base}/api/instances`)).json()) as { items: Record<string, unknown>[] };
    expect(inst.items[0]).toMatchObject({ public_ip: null, provider_resource_id: null, slot: 'warm', lifecycle_state: 'ready' });

    let ov = (await (await fetch(`${base}/api/overview`)).json()) as { integrations: Record<string, { state: string; observed_at: string | null; detail?: string }>; capacity: { observed: Record<string, number>; simulated_count: number } };
    expect(ov.integrations['stackstorm']!.state).toBe('fresh');
    expect(ov.integrations['supabase']!.state).toBe('fresh');
    expect(ov.integrations['prometheus']).toMatchObject({ state: 'unavailable', observed_at: null, detail: 'Prometheus is not configured' });
    expect(ov.integrations['haproxy']).toMatchObject({ state: 'unavailable', observed_at: null, detail: 'not probed in this phase' });
    for (const [name, i] of Object.entries(ov.integrations)) {
      if (i.state === 'fresh') expect(i.observed_at, `${name} fresh without a sample time`).not.toBeNull();
    }
    expect(ov.integrations['providers']).toMatchObject({ state: 'unavailable', observed_at: null, detail: 'no successful reconcile run recorded yet' });
    expect(ov.capacity.observed).toMatchObject({ warm_ready: 1, occupied_digitalocean: 1 });
    expect(inst.items[0]).toMatchObject({ simulated: false });
    expect(ov.capacity.simulated_count).toBe(0);

    // Simulated inventory is listed with its flag but is never counted as observed capacity.
    const simId = (await q(`INSERT INTO ops.instances (slot_key, provider, role, vcpu, memory_mb, lifecycle_state, routing_mode, slot_state, simulated)
             VALUES ('elastic-linode-1', 'linode', 'elastic', 1, 1024, 'ready', 'active', 'bound', true) RETURNING id`)).rows[0].id;
    await q(`UPDATE ops.capacity_slots SET state = 'bound', bound_instance_id = $1 WHERE key = 'elastic-linode-1'`, [simId]);
    const both = (await (await fetch(`${base}/api/instances`)).json()) as { items: { id: string; simulated: boolean }[] };
    expect(both.items.find((i) => i.id === simId)?.simulated).toBe(true);
    expect(both.items.find((i) => i.id === warmId)?.simulated).toBe(false);
    const withSim = (await (await fetch(`${base}/api/overview`)).json()) as typeof ov;
    expect(withSim.capacity.observed).toMatchObject({ warm_ready: 1, elastic: 0, serving: 0, occupied_digitalocean: 1, occupied_linode: 0 });
    expect(withSim.capacity.simulated_count).toBe(1);
    // A reconcile run by the local simulation adapter is not an observation of real providers.
    await q(`INSERT INTO ops.operations (kind, policy_version, status, finished_at, adapter_mode) VALUES ('reconcile', 1, 'succeeded', now() - interval '5 seconds', 'simulation')`);
    ov = (await (await fetch(`${base}/api/overview`)).json()) as typeof ov;
    expect(ov.integrations['providers']).toMatchObject({ state: 'unavailable', observed_at: null, detail: 'only simulated reconcile runs recorded' });
    await q(`INSERT INTO ops.operations (kind, policy_version, status, finished_at) VALUES ('reconcile', 1, 'succeeded', now() - interval '10 seconds')`);
    ov = (await (await fetch(`${base}/api/overview`)).json()) as typeof ov;
    expect(ov.integrations['providers']!.state).toBe('fresh');

    for (const path of ['/api/overview', '/api/instances', '/api/operations', '/api/policies', '/api/backups', '/api/incidents', '/api/tests']) {
      const text = await (await fetch(`${base}${path}`)).text();
      for (const secret of [ST2_KEY, OPS_PW, OPERATOR_PW, '198.51.100.23', 'droplet-123456', st2Url]) expect(text).not.toContain(secret);
    }
    await q(`UPDATE ops.capacity_slots SET state = 'free', bound_instance_id = NULL, lease_operation_id = NULL`);
    await q('DELETE FROM ops.instances');
  });
  it('GET /api/backups lists seeded rows with computed retention and no artifact path', async () => {
    await q(`INSERT INTO ops.backups (capture_status, verification_status, capture_started_at, capture_finished_at)
             VALUES ('captured', 'unverified', '2026-10-01T00:00:00Z', '2026-10-01T00:00:05Z')`);
    await q(`INSERT INTO ops.backups (capture_status, verification_status, capture_started_at) VALUES ('failed', 'unverified', '2026-10-02T00:00:00Z')`);
    const r = await fetch(`${base}/api/backups`);
    expect(r.status).toBe(200);
    const body = (await r.json()) as { items: Record<string, unknown>[] };
    expect(body.items).toHaveLength(2);
    expect(new Date(String(body.items[1]!['retention_until'])).toISOString()).toBe('2026-10-31T00:00:05.000Z');
    expect(body.items[0]!['retention_until']).toBeNull();
    expect(JSON.stringify(body)).not.toContain('artifact_path');
  });

  describe('request body guard', () => {
    const raw = (headers: Record<string, string>, chunks: Buffer[], chunked: boolean) =>
      new Promise<number>((resolve, reject) => {
        const u = new URL(`${base}/api/operations`);
        const total = chunks.reduce((n, c) => n + c.length, 0);
        const req = http.request(
          { host: u.hostname, port: u.port, path: u.pathname, method: 'POST', headers: { ...auth, 'idempotency-key': key(), ...(chunked ? {} : { 'content-length': String(total) }), ...headers } },
          (res) => (res.resume(), resolve(res.statusCode ?? 0)),
        );
        req.on('error', (e) => ((e as { code?: string }).code === 'ECONNRESET' || (e as { code?: string }).code === 'EPIPE' ? resolve(413) : reject(e)));
        for (const c of chunks) req.write(c);
        req.end();
      });
    const big = Buffer.alloc(9 * 1024, 'a');

    it('rejects oversized non-JSON bodies by Content-Length and by streamed bytes', async () => {
      expect(await raw({ 'content-type': 'text/plain' }, [big], false)).toBe(413);
      expect(await raw({ 'content-type': 'text/plain' }, [big.subarray(0, 4096), big.subarray(4096)], true)).toBe(413);
    });

    it('rejects oversized chunked JSON and accepts small chunked JSON', async () => {
      const json = Buffer.from(JSON.stringify({ kind: 'reconcile', reason: 'x'.repeat(9 * 1024) }));
      expect(await raw({ 'content-type': 'application/json' }, [json.subarray(0, 4096), json.subarray(4096)], true)).toBe(413);
      const ok = Buffer.from(JSON.stringify({ kind: 'reconcile' }));
      expect(await raw({ 'content-type': 'application/json' }, [ok.subarray(0, 5), ok.subarray(5)], true)).toBe(202);
    });
  });

  it('idempotency is scoped by (route, key): replay returns the original, mismatch is 422, other routes are independent', async () => {
    const k = key();
    const first = await send('POST', '/api/operations', { kind: 'reconcile' }, auth, k);
    expect(first.status).toBe(202);
    const accepted = (await first.json()) as { operation_id: string; status: string };
    const dispatches = st2.calls.length;

    const replay = await send('POST', '/api/operations', { kind: 'reconcile' }, auth, k);
    expect(replay.status).toBe(202);
    expect(replay.headers.get('idempotent-replayed')).toBe('true');
    expect(await replay.json()).toEqual(accepted);
    expect(st2.calls.length).toBe(dispatches);
    expect((await q('SELECT 1 FROM ops.operations WHERE idempotency_key = $1', [k])).rowCount).toBe(1);

    const reused = await send('POST', '/api/operations', { kind: 'scale_out' }, auth, k);
    expect(reused.status).toBe(422);
    expect(((await reused.json()) as { code?: string }).code).toBe('IDEMPOTENCY_KEY_REUSED');

    const other = await send('POST', '/api/backups', undefined, auth, k);
    expect(other.status).toBe(202);
    expect(((await other.json()) as { operation_id: string }).operation_id).not.toBe(accepted.operation_id);
  });
});
