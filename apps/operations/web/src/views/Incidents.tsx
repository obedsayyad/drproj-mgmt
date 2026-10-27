import { useState } from 'react';
import { get, type Incident, type Page } from '../api';
import { ago, when } from '../format';
import { Empty, useLoad } from '../shared';

export function IncidentsView() {
  const [state, setState] = useState('open');
  const { data, error } = useLoad(() => get<Page<Incident>>(`/api/incidents?limit=50${state ? `&state=${state}` : ''}`), [state], 10_000);
  return (
    <section>
      <div className="head-row">
        <h2>Incidents</h2>
        <label className="field inline">
          <span>Show</span>
          <select value={state} onChange={(e) => setState(e.target.value)}>
            <option value="open">Open</option>
            <option value="resolved">Resolved</option>
            <option value="">All</option>
          </select>
        </label>
      </div>
      {error && <p className="action-error">{error}</p>}
      {data && data.items.length === 0 && <Empty>{state === 'open' ? 'No open incidents.' : 'No incidents recorded.'}</Empty>}
      <ul className="incidents">
        {data?.items.map((i) => (
          <li key={i.id} className={`inc inc-${i.severity}`}>
            <div className="inc-head">
              <strong>{i.description ?? i.fingerprint}</strong>
              <span className="sub">
                {i.severity}, {i.state === 'open' ? `open since ${ago(i.opened_at)}` : `resolved ${when(i.resolved_at)}`}
                {i.source && `, from ${i.source}`}
              </span>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
