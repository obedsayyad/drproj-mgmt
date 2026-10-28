import { describe, expect, it } from 'vitest';
import { probeHaproxy, probePrometheus } from './integrations.js';

const NOW = Date.parse('2026-10-25T12:00:00Z');
const BASE = 'http://prometheus.internal:9090';
const target = (job: string, agoSeconds: number, health = 'up') => ({
  scrapePool: job,
  health,
  lastScrape: new Date(NOW - agoSeconds * 1000).toISOString().replace('Z', '123456Z'), // Prometheus emits ns precision
});
const reply = (activeTargets: unknown[]): typeof fetch => async () =>
  new Response(JSON.stringify({ status: 'success', data: { activeTargets } }), { status: 200 });
const probe = (f: typeof fetch) => probePrometheus(BASE, ['prometheus', 'ops'], 30, NOW, f);

describe('probePrometheus', () => {
  it('is fresh when every expected target is up and scraped within the stale threshold', async () => {
    const r = await probe(reply([target('prometheus', 2), target('ops', 29), target('other', 600)]));
    expect(r).toEqual({ state: 'fresh', observed_at: new Date(NOW - 29_000).toISOString() });
  });

  it('is stale when one target was scraped longer ago than stale_after_seconds', async () => {
    const r = await probe(reply([target('prometheus', 2), target('ops', 42)]));
    expect(r).toMatchObject({ state: 'stale', detail: 'job ops last scrape 42 s ago' });
  });

  it('is stale when a target is down even though it was just attempted', async () => {
    const r = await probe(reply([target('prometheus', 1), target('ops', 1, 'down')]));
    expect(r).toMatchObject({ state: 'stale', detail: 'job ops target is down' });
  });

  it('is stale when an expected job has no active target, and judges every target of a job', async () => {
    expect(await probe(reply([target('prometheus', 1)]))).toMatchObject({ state: 'stale', detail: 'job ops has no active target' });
    expect(await probe(reply([target('prometheus', 1), target('ops', 1), target('ops', 99)]))).toMatchObject({ state: 'stale' });
  });

  it('treats a never-scraped target as stale, not fresh', async () => {
    const never = { scrapePool: 'ops', health: 'unknown', lastScrape: '0001-01-01T00:00:00Z' };
    expect(await probe(reply([target('prometheus', 1), never]))).toMatchObject({ state: 'stale', detail: 'job ops was never scraped' });
  });

  it('is unavailable on HTTP errors, network errors, timeouts and malformed bodies, without leaking the URL', async () => {
    const cases: Array<[typeof fetch, string]> = [
      [async () => new Response('x', { status: 500 }), 'HTTP 500'],
      [async () => { throw new TypeError('connect ECONNREFUSED prometheus.internal:9090'); }, 'unreachable or unreadable response'],
      [async () => { throw Object.assign(new Error('t'), { name: 'TimeoutError' }); }, 'timed out'],
      [async () => new Response('not json', { status: 200 }), 'unreachable or unreadable response'],
      [async () => new Response(JSON.stringify({ status: 'error' }), { status: 200 }), 'unreachable or unreadable response'],
    ];
    for (const [f, detail] of cases) {
      const r = await probe(f);
      expect(r).toEqual({ state: 'unavailable', observed_at: null, detail });
      expect(JSON.stringify(r)).not.toContain('prometheus.internal');
    }
  });

  it('only reads: one GET to the targets endpoint', async () => {
    const calls: Array<[unknown, RequestInit | undefined]> = [];
    await probe(async (u, init) => { calls.push([u, init]); return new Response(JSON.stringify({ status: 'success', data: { activeTargets: [] } })); });
    expect(calls).toHaveLength(1);
    expect(calls[0]![0]).toBe(`${BASE}/api/v1/targets?state=active`);
    expect(calls[0]![1]?.method ?? 'GET').toBe('GET');
  });
});

describe('probeHaproxy', () => {
  it('claims nothing unless the haproxy job is expected', async () => {
    const r = await probeHaproxy(BASE, ['prometheus', 'ops'], 30, NOW, reply([target('haproxy', 1)]));
    expect(r).toEqual({ state: 'unavailable', observed_at: null, detail: 'not probed in this phase' });
  });

  it('is fresh while the haproxy exporter target is up and recent, stale when it is down', async () => {
    expect(await probeHaproxy(BASE, ['prometheus', 'haproxy'], 30, NOW, reply([target('haproxy', 4)]))).toMatchObject({ state: 'fresh' });
    expect(await probeHaproxy(BASE, ['haproxy'], 30, NOW, reply([target('haproxy', 4, 'down')]))).toMatchObject({ state: 'stale', detail: 'job haproxy target is down' });
  });
});
