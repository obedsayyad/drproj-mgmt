import { get, type Overview } from '../api';
import { ago } from '../format';
import { useLoad } from '../shared';

const INTEGRATIONS: [string, string, string][] = [
  ['haproxy', 'Traffic router (HAProxy)', 'Sends each visitor to a server that is up and keeps the busy or broken ones out of the way.'],
  ['prometheus', 'Metrics (Prometheus)', 'Collects the numbers (load, errors, response time) that decide when to add or remove servers.'],
  ['stackstorm', 'Automation (StackStorm)', 'The robot that carries out failover, adding and removing servers, and backups.'],
  ['supabase', 'Database (Supabase)', "Where the relief requests and this dashboard's records are stored."],
  ['providers', 'Cloud accounts (DigitalOcean + Linode)', 'The two companies we rent extra servers from when traffic grows.'],
];
const STATE_WORD = { fresh: 'OK', stale: 'Slow to answer', unavailable: 'Not reachable' } as const;

export function OverviewView({ onOpenProblems }: { onOpenProblems?: () => void }) {
  const { data, error } = useLoad(() => get<Overview>('/api/overview'), [], 10_000);
  if (!data) return <p className={error ? 'action-error' : 'empty'}>{error ?? 'Loading…'}</p>;
  const d = data.capacity.desired;
  const o = data.capacity.observed;
  return (
    <div className="overview">
      {error && <p className="action-error">Refresh failed: {error}. Showing data from {ago(data.observed_at)}.</p>}
      <section aria-labelledby="signals">
        <h2 id="signals">Health checks</h2>
        <p className="hint">Live view of the relief app's servers, traffic routing and backups. Green means we checked the tool just now and it answered.</p>
        <ul className="signals">
          {INTEGRATIONS.map(([key, label, about]) => {
            const s = data.integrations[key];
            const state = s?.state ?? 'unavailable';
            return (
              <li key={key} className={`signal signal-${state}`} title={about}>
                <span className="signal-name">{label}</span>
                <span className="signal-state">
                  {STATE_WORD[state]}
                  {state === 'fresh' && s?.observed_at && <small> · checked {ago(s.observed_at)}</small>}
                </span>
                <span className="signal-about">{about}</span>
                {s?.detail && <span className="signal-detail">{s.detail}</span>}
              </li>
            );
          })}
        </ul>
      </section>

      <div className="split">
        <section aria-labelledby="capacity">
          <h2 id="capacity">Servers</h2>
          <p className="hint">Extra servers are added one at a time when the main server is busy, and removed when it's quiet.</p>
          <table className="kv">
            <thead>
              <tr>
                <th scope="col"></th>
                <th scope="col">Target</th>
                <th scope="col">Right now</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <th scope="row">Main server</th>
                <td>{d['primary']}</td>
                <td className={o['primary_ready']! < d['primary']! ? 'short' : ''}>{o['primary_ready']}</td>
              </tr>
              <tr>
                <th scope="row">Backup server (warm standby)</th>
                <td>{d['warm']}</td>
                <td className={o['warm_ready']! < d['warm']! ? 'short' : ''}>{o['warm_ready']}</td>
              </tr>
              <tr>
                <th scope="row">Servers currently taking traffic</th>
                <td>—</td>
                <td>{o['serving']}</td>
              </tr>
              <tr>
                <th scope="row">Extra cloud servers on DigitalOcean (max 5)</th>
                <td>up to {d['cap_digitalocean']}</td>
                <td>{o['occupied_digitalocean']}</td>
              </tr>
              <tr>
                <th scope="row">Extra cloud servers on Linode (max 5)</th>
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
          <h2 id="status">At a glance</h2>
          <dl className="facts">
            <div>
              <dt>
                <button type="button" className="linklike" onClick={onOpenProblems}>
                  Open problems
                </button>
              </dt>
              <dd className={data.incidents.critical > 0 ? 'short' : ''}>
                {data.incidents.open}
                {data.incidents.critical > 0 && ` (${data.incidents.critical} critical)`}
              </dd>
            </div>
            <div>
              <dt>Last backup</dt>
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
              <dt>Settings version</dt>
              <dd>version {data.active_policy_version}</dd>
            </div>
          </dl>
        </section>
      </div>
    </div>
  );
}
