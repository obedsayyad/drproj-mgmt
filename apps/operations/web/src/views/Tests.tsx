import { useState, type FormEvent } from 'react';
import { get, type ApiError, type Page, type PolicyVersion, type TestRun } from '../api';
import { ago, human } from '../format';
import { ActionError, asApiError, Empty, StatusWord, useLoad, useOperator } from '../shared';

const SCENARIOS = ['smoke', 'baseline', 'compute_pressure', 'sustained_surge', 'recovery'];
const ENDED = ['succeeded', 'failed', 'cancelled'];

export function TestsView() {
  const { change, password, lastOperation } = useOperator();
  const { data, error, reload } = useLoad(() => get<Page<TestRun>>('/api/tests?limit=30'), [lastOperation], 5000);
  const { data: policies } = useLoad(() => get<Page<PolicyVersion>>('/api/policies?limit=5'), []);
  const demo = policies?.items.find((v) => v.status === 'active')?.configuration.demo;
  const maxRate = demo?.max_rate_rps ?? 200;
  const maxDur = demo?.max_duration_seconds ?? 600;
  const [scenario, setScenario] = useState('baseline');
  const [rate, setRate] = useState('10');
  const [duration, setDuration] = useState('120');
  const [err, setErr] = useState<ApiError | null>(null);
  const running = data?.items.find((t) => !ENDED.includes(t.status));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr(null);
    try {
      await change('POST', '/api/tests', { scenario, rate_rps: Number(rate), duration_seconds: Number(duration) });
      reload();
    } catch (x) {
      setErr(asApiError(x));
    }
  };
  const stop = async (id: string) => {
    setErr(null);
    try {
      await change('POST', `/api/tests/${id}/stop`);
      reload();
    } catch (x) {
      setErr(asApiError(x));
    }
  };
  const rateOk = Number(rate) >= 1 && Number(rate) <= maxRate;
  const durOk = Number(duration) >= 1 && Number(duration) <= maxDur;

  return (
    <div className="two-col">
      <form className="panel" onSubmit={(e) => void submit(e)}>
        <h2>Start a load test</h2>
        <p className="hint">k6 runs from the test server against the public app address at a fixed rate.</p>
        <label className="field">
          <span>Scenario</span>
          <select value={scenario} onChange={(e) => setScenario(e.target.value)}>
            {SCENARIOS.map((s) => (
              <option key={s} value={s}>
                {human(s)}
              </option>
            ))}
          </select>
        </label>
        <div className="row-fields">
          <label className="field">
            <span>Rate (1–{maxRate} req/s)</span>
            <input inputMode="numeric" value={rate} aria-invalid={!rateOk} onChange={(e) => setRate(e.target.value.replace(/\D/g, ''))} />
          </label>
          <label className="field">
            <span>Duration (1–{maxDur} s)</span>
            <input inputMode="numeric" value={duration} aria-invalid={!durOk} onChange={(e) => setDuration(e.target.value.replace(/\D/g, ''))} />
          </label>
        </div>
        <ActionError error={err} />
        <button className="primary" disabled={!password || !rateOk || !durOk || Boolean(running)} title={running ? 'A test is already running' : undefined}>
          Start test
        </button>
      </form>
      <section>
        <h2>Runs</h2>
        {error && <p className="action-error">{error}</p>}
        {data && data.items.length === 0 && <Empty>No load tests yet.</Empty>}
        {data && data.items.length > 0 && (
          <table className="grid">
            <thead>
              <tr>
                <th scope="col">Scenario</th>
                <th scope="col">Load</th>
                <th scope="col">Status</th>
                <th scope="col">Result</th>
                <th scope="col"></th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((t) => (
                <tr key={t.id}>
                  <td>
                    {human(t.scenario)}
                    <span className="sub">{ago(t.started_at)}</span>
                  </td>
                  <td>
                    {t.rate_rps} req/s for {t.duration_seconds} s
                  </td>
                  <td>
                    <StatusWord status={t.status} />
                  </td>
                  <td>{t.summary ? `p95 ${t.summary.p95_ms ?? '—'} ms, errors ${((t.summary.error_rate ?? 0) * 100).toFixed(2)}%` : '—'}</td>
                  <td>
                    {!ENDED.includes(t.status) && (
                      <button className="secondary" disabled={!password} onClick={() => void stop(t.id)}>
                        Stop
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
