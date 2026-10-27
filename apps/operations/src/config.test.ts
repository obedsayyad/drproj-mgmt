import { describe, expect, it } from 'vitest';
import { loadConfig } from './config.js';

const base = { SUPABASE_OPS_URL: 'postgres://h/db' };

describe('operator authentication at startup', () => {
  it('refuses to start without a password unless proxy auth is trusted explicitly', () => {
    expect(() => loadConfig(base)).toThrow(/OPS_OPERATOR_PASSWORD is required unless OPS_TRUST_PROXY_AUTH=true/);
    expect(() => loadConfig({ ...base, OPS_TRUST_PROXY_AUTH: 'false' })).toThrow(/OPS_OPERATOR_PASSWORD/);
  });

  it('starts with a password, or with the explicit proxy-auth opt-in', () => {
    expect(loadConfig({ ...base, OPS_OPERATOR_PASSWORD: 'twelve-chars-ok' })).toMatchObject({ operatorPassword: 'twelve-chars-ok', trustProxyAuth: false });
    expect(loadConfig({ ...base, OPS_TRUST_PROXY_AUTH: 'true' })).toMatchObject({ operatorPassword: null, trustProxyAuth: true });
  });

  it('does not echo the password in errors', () => {
    try {
      loadConfig({ ...base, OPS_OPERATOR_PASSWORD: 'short' });
    } catch (e) {
      expect(String(e)).not.toContain('short');
    }
  });
});

describe('OPS_DB_POOL_MAX', () => {
  const withPw = { ...base, OPS_OPERATOR_PASSWORD: 'twelve-chars-ok' };
  it('refuses values outside 1..5 instead of adjusting them', () => {
    for (const bad of ['0', '6', '9', '-1', '2.5']) {
      expect(() => loadConfig({ ...withPw, OPS_DB_POOL_MAX: bad })).toThrow(/OPS_DB_POOL_MAX/);
    }
    expect(loadConfig({ ...withPw, OPS_DB_POOL_MAX: '5' }).dbPoolMax).toBe(5);
    expect(loadConfig({ ...withPw, OPS_DB_POOL_MAX: '1' }).dbPoolMax).toBe(1);
    expect(loadConfig(withPw).dbPoolMax).toBe(3);
  });
});

describe('OPS_METRICS_PORT', () => {
  const withPw = { ...base, OPS_OPERATOR_PASSWORD: 'twelve-chars-ok' };
  it('defaults to 9465 and may not collide with the API port', () => {
    expect(loadConfig(withPw).metricsPort).toBe(9465);
    expect(loadConfig({ ...withPw, OPS_METRICS_PORT: '9500' }).metricsPort).toBe(9500);
    expect(() => loadConfig({ ...withPw, OPS_PORT: '9465' })).toThrow(/OPS_METRICS_PORT must differ/);
    expect(() => loadConfig({ ...withPw, OPS_METRICS_PORT: '70000' })).toThrow(/OPS_METRICS_PORT/);
  });
});
