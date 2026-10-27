import { get, type Overview } from '../api';
import { ago } from '../format';
import { useLoad } from '../shared';

const INTEGRATIONS: [string, string][] = [
  ['haproxy', 'HAProxy routing'],
  ['prometheus', 'Prometheus metrics'],
  ['stackstorm', 'StackStorm automation'],
  ['supabase', 'Supabase database'],
  ['providers', 'Cloud providers'],
];
const STATE_WORD = { fresh: 'Fresh', stale: 'Stale', unavailable: 'Unavailable' } as const;

export function OverviewView() {
  const { data, error } = useLoad(() => get<Overview>('/api/overview'), [], 10_000);
  if (!data) return <p className={error ? 'action-error' : 'empty'}>{error ?? 'Loading…'}</p>;
  const d = data.capacity.desired;
  const o = data.capacity.observed;
  return (
    <div className="overview">
      {error && <p className="action-error">Refresh failed: {error}. Showing data from {ago(data.observed_at)}.</p>}
      <section aria-labelledby="signals">
        <h2 id="signals">Integrations</h2>
        <p className="hint">Each tool is checked directly. Anything not reporting is shown as it is, never assumed healthy.</p>
        <ul className="signals">
          {INTEGRATIONS.map(([key, label]) => {
            const s = data.integrations[key];
            const state = s?.state ?? 'unavailable';
            return (
              <li key={key} className={`signal signal-${state}`}>
                <span className="signal-name">{label}</span>
                <span className="signal-state">{STATE_WORD[state]}</span>
                <span className="signal-detail">{s?.detail ?? (s?.observed_at ? `checked ${ago(s.observed_at)}` : '')}</span>
              </li>
            );
          })}
        </ul>
      </section>

      <div className="split">
        <section aria-labelledby="capacity">
          <h2 id="capacity">Capacity</h2>
          <table className="kv">
            <thead>
              <tr>
                <th scope="col"></th>
                <th scope="col">Wanted</th>
                <th scope="col">Now</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <th scope="row">Primary server ready</th>
                <td>{d['primary']}</td>
                <td className={o['primary_ready']! < d['primary']! ? 'short' : ''}>{o['primary_ready']}</td>
              </tr>
              <tr>
                <th scope="row">Warm standby ready</th>
                <td>{d['warm']}</td>
                <td className={o['warm_ready']! < d['warm']! ? 'short' : ''}>{o['warm_ready']}</td>
              </tr>
              <tr>
                <th scope="row">Servers taking traffic</th>
                <td>—</td>
                <td>{o['serving']}</td>
              </tr>
              <tr>
                <th scope="row">DigitalOcean slots used</th>
                <td>up to {d['cap_digitalocean']}</td>
                <td>{o['occupied_digitalocean']}</td>
              </tr>
              <tr>
                <th scope="row">Linode slots used</th>
                <td>up to {d['cap_linode']}</td>
                <td>{o['occupied_linode']}</td>
              </tr>
              {data.capacity.simulated_count > 0 && (
                <tr>
                  <th scope="row">
                    Simulated servers <span className="badge-simulated">SIMULATED</span>
                  </th>
                  <td>—</td>
                  <td>{data.capacity.simulated_count}</td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
        <section aria-labelledby="status">
          <h2 id="status">Status</h2>
          <dl className="facts">
            <div>
              <dt>Open incidents</dt>
              <dd className={data.incidents.critical > 0 ? 'short' : ''}>
                {data.incidents.open}
                {data.incidents.critical > 0 && ` (${data.incidents.critical} critical)`}
              </dd>
            </div>
            <div>
              <dt>Latest backup</dt>
              <dd>
                {data.latest_backup ? (
                  <>
                    {ago(data.latest_backup.capture_finished_at)}, {data.latest_backup.verification_status}
                  </>
                ) : (
                  'none yet'
                )}
              </dd>
            </div>
            <div>
              <dt>Active policy</dt>
              <dd>version {data.active_policy_version}</dd>
            </div>
          </dl>
        </section>
      </div>
    </div>
  );
}
