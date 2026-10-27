import { useState, type FormEvent } from 'react';
import { BackupsView } from './views/Backups';
import { IncidentsView } from './views/Incidents';
import { InstancesView } from './views/Instances';
import { OperationsView } from './views/Operations';
import { OverviewView } from './views/Overview';
import { PoliciesView } from './views/Policies';
import { TestsView } from './views/Tests';
import { OperatorProvider, useOperator } from './shared';

const TABS = [
  ['overview', 'Overview'],
  ['operations', 'Operations'],
  ['instances', 'Servers'],
  ['policies', 'Policy'],
  ['backups', 'Backups'],
  ['tests', 'Load tests'],
  ['incidents', 'Incidents'],
] as const;
type Tab = (typeof TABS)[number][0];

export function App() {
  const [tab, setTab] = useState<Tab>('overview');
  return (
    <OperatorProvider onOperation={() => setTab('operations')}>
      <div className="frame">
        <header className="top">
          <div>
            <h1>Recovery control</h1>
            <p>Disaster-relief demo: routing, capacity, backups and policy.</p>
          </div>
          <OperatorLock />
        </header>
        <nav className="tabs" aria-label="Sections">
          {TABS.map(([id, label]) => (
            <button key={id} className={tab === id ? 'on' : ''} aria-current={tab === id ? 'page' : undefined} onClick={() => setTab(id)}>
              {label}
            </button>
          ))}
        </nav>
        <main>
          {tab === 'overview' && <OverviewView />}
          {tab === 'operations' && <OperationsView />}
          {tab === 'instances' && <InstancesView />}
          {tab === 'policies' && <PoliciesView />}
          {tab === 'backups' && <BackupsView />}
          {tab === 'tests' && <TestsView />}
          {tab === 'incidents' && <IncidentsView />}
        </main>
      </div>
    </OperatorProvider>
  );
}

function OperatorLock() {
  const { password, setPassword } = useOperator();
  const [value, setValue] = useState('');
  if (password) {
    return (
      <div className="lock unlocked">
        <span>Changes unlocked</span>
        <button className="link" onClick={() => setPassword(null)}>
          Lock
        </button>
      </div>
    );
  }
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (value) setPassword(value);
    setValue('');
  };
  return (
    <form className="lock" onSubmit={submit}>
      <label>
        <span>Operator password</span>
        <input type="password" autoComplete="current-password" value={value} onChange={(e) => setValue(e.target.value)} />
      </label>
      <button type="submit" className="primary" disabled={!value}>
        Unlock changes
      </button>
    </form>
  );
}
