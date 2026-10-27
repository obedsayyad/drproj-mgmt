import { useState } from 'react';
import { get, type Instance, type Page } from '../api';
import { ago, human } from '../format';
import { Empty, useLoad } from '../shared';

export function InstancesView() {
  const [lifecycle, setLifecycle] = useState('');
  const { data, error } = useLoad(
    () => get<Page<Instance>>(`/api/instances?limit=100${lifecycle ? `&lifecycle_state=${lifecycle}` : ''}`),
    [lifecycle],
    10_000,
  );
  return (
    <section>
      <div className="head-row">
        <h2>Servers</h2>
        <label className="field inline">
          <span>Lifecycle</span>
          <select value={lifecycle} onChange={(e) => setLifecycle(e.target.value)}>
            <option value="">All except deleted</option>
            {['ready', 'provisioning', 'deploying', 'draining', 'deleting', 'failed', 'unknown', 'deleted'].map((s) => (
              <option key={s} value={s}>
                {human(s)}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="hint">Addresses and provider IDs are not shown on this public page.</p>
      {error && <p className="action-error">{error}</p>}
      {data && data.items.filter((i) => lifecycle || i.lifecycle_state !== 'deleted').length === 0 && (
        <Empty>No servers recorded yet. The primary is registered at deployment; cloud servers appear when StackStorm creates them.</Empty>
      )}
      {data && data.items.length > 0 && (
        <table className="grid">
          <thead>
            <tr>
              <th scope="col">Role</th>
              <th scope="col">Provider and slot</th>
              <th scope="col">Lifecycle</th>
              <th scope="col">Routing</th>
              <th scope="col">Size</th>
              <th scope="col">Last seen</th>
            </tr>
          </thead>
          <tbody>
            {data.items
              .filter((i) => lifecycle || i.lifecycle_state !== 'deleted')
              .map((i) => (
                <tr key={i.id}>
                  <td>
                    {human(i.role)}
                    {i.simulated && <span className="badge-simulated" title="Created by a local simulation adapter; not a real server">SIMULATED</span>}
                  </td>
                  <td>
                    {human(i.provider)}
                    {i.slot && <span className="sub">{i.slot}</span>}
                  </td>
                  <td>
                    <span className={`st st-${i.lifecycle_state}`}>{i.lifecycle_state}</span>
                    {i.health_state && <span className="sub">{i.health_state}</span>}
                  </td>
                  <td>
                    {human(i.routing_mode)}
                    {i.routing_mode !== 'none' && <span className="sub">weight {i.routing_weight}</span>}
                  </td>
                  <td>{i.vcpu ? `${i.vcpu} vCPU / ${((i.memory_mb ?? 0) / 1024).toFixed(0)} GB` : '—'}</td>
                  <td>{ago(i.last_seen_at)}</td>
                </tr>
              ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
