import { useState } from 'react';
import { get, type ApiError, type Backup, type Page } from '../api';
import { ago, mb, when } from '../format';
import { ActionError, asApiError, Empty, StatusWord, useLoad, useOperator } from '../shared';

export function BackupsView() {
  const { change, password, lastOperation } = useOperator();
  const { data, error, reload } = useLoad(() => get<Page<Backup>>('/api/backups?limit=50'), [lastOperation], 10_000);
  const [err, setErr] = useState<ApiError | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const act = async (method: 'POST', path: string, body?: unknown) => {
    setErr(null);
    try {
      await change(method, path, body);
      setConfirm(null);
      reload();
    } catch (x) {
      setErr(asApiError(x));
    }
  };
  const fresh = (b: Backup) => b.verification_status === 'verified' && b.verified_at && Date.now() - new Date(b.verified_at).getTime() < 7 * 86400_000;
  return (
    <section>
      <div className="head-row">
        <h2>Backups</h2>
        <button className="primary" disabled={!password} onClick={() => void act('POST', '/api/backups')} title={password ? undefined : 'Unlock changes first'}>
          Back up now
        </button>
      </div>
      <p className="hint">Daily at 00:00 UTC. A restore needs a backup verified within the last 7 days; it pauses app writes while it runs.</p>
      <ActionError error={err} />
      {error && <p className="action-error">{error}</p>}
      {data && data.items.length === 0 && <Empty>No backups yet. The first runs at 00:00 UTC, or start one now.</Empty>}
      {data && data.items.length > 0 && (
        <table className="grid">
          <thead>
            <tr>
              <th scope="col">Taken</th>
              <th scope="col">Capture</th>
              <th scope="col">Verified</th>
              <th scope="col">Size</th>
              <th scope="col">Kept until</th>
              <th scope="col"></th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((b) => (
              <tr key={b.id}>
                <td title={when(b.capture_started_at)}>
                  {ago(b.capture_finished_at ?? b.capture_started_at)}
                  {b.is_safety_backup && <span className="sub">safety copy before a restore</span>}
                </td>
                <td>
                  <StatusWord status={b.capture_status} />
                  {b.error_message && <span className="sub bad">{b.error_message}</span>}
                </td>
                <td>
                  <StatusWord status={b.verification_status} />
                  {b.verified_at && <span className="sub">{ago(b.verified_at)}</span>}
                </td>
                <td>{mb(b.bytes)}</td>
                <td>{when(b.retention_until)}</td>
                <td className="actions-cell">
                  {b.capture_status === 'captured' && (
                    <button className="secondary" disabled={!password} onClick={() => void act('POST', `/api/backups/${b.id}/verify`)}>
                      Verify
                    </button>
                  )}
                  {fresh(b) &&
                    (confirm === b.id ? (
                      <span className="confirm">
                        <button className="danger" onClick={() => void act('POST', '/api/restores', { backup_id: b.id })}>
                          Restore app data
                        </button>
                        <button className="link" onClick={() => setConfirm(null)}>
                          Cancel
                        </button>
                      </span>
                    ) : (
                      <button className="secondary" disabled={!password} onClick={() => setConfirm(b.id)}>
                        Restore…
                      </button>
                    ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
