import { useMemo, useState, type FormEvent } from 'react';
import { get, type ApiError, type Page, type PolicyVersion } from '../api';
import { ago, when } from '../format';
import { ActionError, asApiError, StatusWord, useLoad, useOperator } from '../shared';

/** The knobs people actually change during the demo; everything else is shown read-only. */
const FIELDS: { path: [string, string, string?]; label: string; unit?: string; kind?: 'provider' }[] = [
  { path: ['scale_out', 'cpu_percent'], label: 'Add capacity above CPU', unit: '%' },
  { path: ['scale_out', 'sustained_seconds'], label: '…sustained for', unit: 's' },
  { path: ['scale_out', 'cooldown_seconds'], label: 'Cooldown after each added VM', unit: 's' },
  { path: ['scale_in', 'cpu_percent'], label: 'Remove capacity below CPU', unit: '%' },
  { path: ['scale_in', 'sustained_seconds'], label: '…sustained for', unit: 's' },
  { path: ['capacity', 'max_cloud_vms_per_provider', 'digitalocean'], label: 'DigitalOcean VM cap', unit: 'VMs' },
  { path: ['capacity', 'max_cloud_vms_per_provider', 'linode'], label: 'Linode VM cap', unit: 'VMs' },
  { path: ['capacity', 'warm_provider'], label: 'Warm standby provider', kind: 'provider' },
  { path: ['backup', 'retention_days'], label: 'Keep backups for', unit: 'days' },
  { path: ['demo', 'max_rate_rps'], label: 'Load test rate limit', unit: 'req/s' },
];

const read = (cfg: Record<string, unknown>, p: (string | undefined)[]) =>
  p.filter(Boolean).reduce<unknown>((o, k) => (o as Record<string, unknown> | undefined)?.[k!], cfg);

function setDeep(target: Record<string, unknown>, p: string[], v: unknown) {
  let o = target;
  p.slice(0, -1).forEach((k) => (o = (o[k] ??= {}) as Record<string, unknown>));
  o[p.at(-1)!] = v;
}

export function PoliciesView() {
  const { lastOperation } = useOperator();
  const { data, error, reload } = useLoad(() => get<Page<PolicyVersion>>('/api/policies?limit=20'), [lastOperation], 5000);
  const active = data?.items.find((v) => v.status === 'active');
  const pending = data?.items.find((v) => v.status === 'pending');
  if (!data) return <p className={error ? 'action-error' : 'empty'}>{error ?? 'Loading…'}</p>;
  if (!active) return <p className="action-error">No active policy. Run the policy seed on the management host.</p>;
  return (
    <div className="two-col">
      <div>
        {pending ? (
          <div className="notice">
            Version {pending.version} is being activated ({pending.activation_phase ?? 'pending'}). Version {active.version} stays in force until it succeeds.
          </div>
        ) : null}
        <EditForm key={active.version} active={active} nextVersion={(data.items[0]?.version ?? active.version) + 1} disabled={Boolean(pending)} onDone={reload} />
        <details className="panel">
          <summary>Full active policy (version {active.version})</summary>
          <pre>{JSON.stringify(active.configuration, null, 2)}</pre>
        </details>
      </div>
      <aside>
        <h2>History</h2>
        <ol className="versions">
          {data.items.map((v) => (
            <li key={v.version}>
              <strong>Version {v.version}</strong> <StatusWord status={v.status} />
              <span className="sub" title={when(v.created_at)}>
                created {ago(v.created_at)}
                {v.activated_at && `, active since ${when(v.activated_at)}`}
              </span>
              {v.status === 'failed' && v.apply_result && <span className="sub bad">{JSON.stringify(v.apply_result)}</span>}
            </li>
          ))}
        </ol>
      </aside>
    </div>
  );
}

function EditForm({ active, nextVersion, disabled, onDone }: { active: PolicyVersion; nextVersion: number; disabled: boolean; onDone: () => void }) {
  const { change, password } = useOperator();
  const initial = useMemo(() => Object.fromEntries(FIELDS.map((f) => [f.path.join('.'), String(read(active.configuration, f.path))])), [active]);
  const [values, setValues] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [done, setDone] = useState(false);
  const dirty = FIELDS.filter((f) => values[f.path.join('.')] !== initial[f.path.join('.')]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const changes: Record<string, unknown> = {};
    for (const f of dirty) {
      const raw = values[f.path.join('.')]!;
      setDeep(changes, f.path.filter(Boolean) as string[], f.kind === 'provider' ? raw : Number(raw));
    }
    setBusy(true);
    setErr(null);
    try {
      await change('PATCH', '/api/policies', { expected_version: active.version, changes });
      setDone(true);
      onDone();
    } catch (x) {
      setErr(asApiError(x));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="panel" onSubmit={(e) => void submit(e)}>
      <h2>Change policy</h2>
      <p className="hint">
        Saving creates version {nextVersion}. The API checks the rules first; StackStorm then applies it to Prometheus, HAProxy and its own
        timers, and only then makes it active.
      </p>
      <div className="policy-fields">
        {FIELDS.map((f) => {
          const k = f.path.join('.');
          return (
            <label key={k} className={`field ${values[k] !== initial[k] ? 'changed' : ''}`}>
              <span>{f.label}</span>
              {f.kind === 'provider' ? (
                <select value={values[k]} onChange={(e) => setValues({ ...values, [k]: e.target.value })}>
                  <option value="digitalocean">DigitalOcean</option>
                  <option value="linode">Linode</option>
                </select>
              ) : (
                <span className="with-unit">
                  <input inputMode="numeric" value={values[k]} onChange={(e) => setValues({ ...values, [k]: e.target.value.replace(/[^\d.]/g, '') })} />
                  <span>{f.unit}</span>
                </span>
              )}
            </label>
          );
        })}
      </div>
      <ActionError error={err} />
      {done && !err && <p className="ok-msg">Change requested. Follow it on the Operations tab.</p>}
      <button className="primary" disabled={busy || disabled || dirty.length === 0 || !password} title={password ? undefined : 'Unlock changes first'}>
        {busy ? 'Saving…' : dirty.length ? `Save ${dirty.length} change${dirty.length > 1 ? 's' : ''}` : 'No changes'}
      </button>
    </form>
  );
}
