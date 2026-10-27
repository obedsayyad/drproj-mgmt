import { useEffect, useState, type FormEvent } from 'react';
import { get, type OpEvent, type Operation, type Page } from '../api';
import { ago, kindLabel, when } from '../format';
import { ActionError, asApiError, Empty, StatusWord, useLoad, useOperator } from '../shared';
import type { ApiError } from '../api';

const COMMANDS = ['activate_standby', 'scale_out', 'scale_in', 'standby_repair', 'failback', 'reconcile'];

export function OperationsView() {
  const { lastOperation, showOperation } = useOperator();
  const { data, error, reload } = useLoad(() => get<Page<Operation>>('/api/operations?limit=30'), [lastOperation], 5000);
  return (
    <div className="two-col">
      <div>
        <RequestForm onDone={reload} />
        <h2>Recent operations</h2>
        {error && <p className="action-error">{error}</p>}
        {data && data.items.length === 0 && <Empty>No operations yet. Requests from this dashboard and automation appear here.</Empty>}
        {data && data.items.length > 0 && (
          <table className="grid">
            <thead>
              <tr>
                <th scope="col">Operation</th>
                <th scope="col">Status</th>
                <th scope="col">Requested</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((op) => (
                <tr key={op.id} className={lastOperation === op.id ? 'sel' : ''}>
                  <td>
                    <button className="link" onClick={() => showOperation(op.id)}>
                      {kindLabel(op.kind)}
                    </button>
                    {op.current_step && <span className="sub">{op.current_step.replaceAll('_', ' ')}</span>}
                  </td>
                  <td>
                    <StatusWord status={op.status} />
                  </td>
                  <td title={when(op.created_at)}>{ago(op.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <aside>{lastOperation ? <OperationDetail key={lastOperation} id={lastOperation} /> : <Empty>Pick an operation to follow its progress.</Empty>}</aside>
    </div>
  );
}

function RequestForm({ onDone }: { onDone: () => void }) {
  const { change, password } = useOperator();
  const [kind, setKind] = useState(COMMANDS[0]!);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await change('POST', '/api/operations', { kind, ...(reason.trim() ? { reason: reason.trim() } : {}) });
      setReason('');
      onDone();
    } catch (x) {
      setErr(asApiError(x));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="panel" onSubmit={(e) => void submit(e)}>
      <h2>Request an operation</h2>
      <p className="hint">StackStorm checks current state, capacity and policy before it changes anything.</p>
      <div className="row-fields">
        <label className="field">
          <span>Operation</span>
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {COMMANDS.map((k) => (
              <option key={k} value={k}>
                {kindLabel(k)}
              </option>
            ))}
          </select>
        </label>
        <label className="field grow">
          <span>Reason (optional)</span>
          <input value={reason} maxLength={1000} onChange={(e) => setReason(e.target.value)} placeholder="Shown in the audit log" />
        </label>
      </div>
      <ActionError error={err} />
      <button className="primary" disabled={busy || !password} title={password ? undefined : 'Unlock changes first'}>
        {busy ? 'Requesting…' : 'Request operation'}
      </button>
    </form>
  );
}

function OperationDetail({ id }: { id: string }) {
  const { data: op } = useLoad(() => get<Operation>(`/api/operations/${id}`), [id], 3000);
  const [events, setEvents] = useState<OpEvent[]>([]);
  const [streamState, setStreamState] = useState<'live' | 'ended' | 'retrying'>('live');
  useEffect(() => {
    // EventSource resends Last-Event-ID on reconnect; the server replays from there.
    const es = new EventSource(`/api/operations/${id}/events`);
    const onEvent = (m: MessageEvent<string>) => {
      const e = JSON.parse(m.data) as OpEvent;
      setEvents((prev) => (prev.some((p) => p.sequence === e.sequence) ? prev : [...prev, e]));
      setStreamState('live');
    };
    es.onmessage = onEvent;
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED) setStreamState('ended');
      else setStreamState('retrying');
    };
    return () => es.close();
  }, [id]);
  if (!op) return <Empty>Loading…</Empty>;
  return (
    <section className="panel detail">
      <h2>{kindLabel(op.kind)}</h2>
      <dl className="facts">
        <div>
          <dt>Status</dt>
          <dd>
            <StatusWord status={op.status} />
          </dd>
        </div>
        <div>
          <dt>Policy version</dt>
          <dd>{op.policy_version}</dd>
        </div>
        <div>
          <dt>StackStorm run</dt>
          <dd>{op.stackstorm_execution_id ? <code>{op.stackstorm_execution_id}</code> : 'not started'}</dd>
        </div>
      </dl>
      {Object.keys(op.parameters).length > 0 && (
        <details>
          <summary>Inputs</summary>
          <pre>{JSON.stringify(op.parameters, null, 2)}</pre>
        </details>
      )}
      <h3>
        Progress <span className="sub">{streamState === 'live' ? 'live' : streamState === 'retrying' ? 'reconnecting…' : ''}</span>
      </h3>
      <ol className="timeline">
        {events.map((e) => (
          <li key={e.sequence} className={`ev ev-${e.measurements.status ?? 'none'}`}>
            <span className="ev-type">{(e.measurements.step ?? e.event_type.replace('operation.', '')).replaceAll('_', ' ')}</span>
            {e.measurements.message && <span className="ev-msg">{e.measurements.message}</span>}
            <time dateTime={e.observed_at}>{when(e.observed_at)}</time>
          </li>
        ))}
      </ol>
      {op.result && (
        <details open>
          <summary>Result</summary>
          <pre>{JSON.stringify(op.result, null, 2)}</pre>
        </details>
      )}
    </section>
  );
}
