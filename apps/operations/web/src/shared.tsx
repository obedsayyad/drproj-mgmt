import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';
import { ApiError, mutate } from './api';

interface OperatorCtx {
  password: string | null;
  setPassword: (p: string | null) => void;
  /** Runs a change; returns the operation id or throws a readable ApiError. */
  change: (method: 'POST' | 'PATCH', path: string, body?: unknown) => Promise<string>;
  lastOperation: string | null;
  showOperation: (id: string | null) => void;
}
const Ctx = createContext<OperatorCtx | null>(null);
export const useOperator = () => useContext(Ctx)!;

export function OperatorProvider({ children, onOperation }: { children: ReactNode; onOperation: (id: string) => void }) {
  // Kept in memory only: a reload asks again. Never written to storage.
  const [password, setPassword] = useState<string | null>(null);
  const [lastOperation, showOperation] = useState<string | null>(null);
  const change = useCallback(
    async (method: 'POST' | 'PATCH', path: string, body?: unknown) => {
      if (!password) throw new ApiError(401, 'LOCKED', 'Enter the operator password to make changes.');
      try {
        const r = await mutate(method, path, password, body);
        showOperation(r.operation_id);
        onOperation(r.operation_id);
        return r.operation_id;
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          setPassword(null);
          throw new ApiError(401, e.code, 'The operator password was not accepted. Enter it again.');
        }
        throw e;
      }
    },
    [password, onOperation],
  );
  return <Ctx.Provider value={{ password, setPassword, change, lastOperation, showOperation }}>{children}</Ctx.Provider>;
}

/** Loads data, optionally refreshing on an interval; keeps the last good data on refresh errors. */
export function useLoad<T>(load: () => Promise<T>, deps: unknown[], refreshMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    const run = () =>
      load()
        .then((d) => live && (setData(d), setError(null)))
        .catch((e: unknown) => live && setError(e instanceof Error ? e.message : 'Failed to load.'));
    void run();
    const t = refreshMs ? setInterval(run, refreshMs) : undefined;
    return () => {
      live = false;
      if (t) clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { data, error, reload: () => setTick((n) => n + 1) };
}

export function ActionError({ error }: { error: ApiError | null }) {
  if (!error) return null;
  return (
    <div className="action-error" role="alert">
      <p>{error.message}</p>
      {error.errors.length > 0 && (
        <ul>
          {error.errors.map((e, i) => (
            <li key={i}>
              <code>{e.field}</code> {e.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function StatusWord({ status }: { status: string }) {
  return <span className={`st st-${status}`}>{status.replaceAll('_', ' ')}</span>;
}

export const asApiError = (e: unknown) => (e instanceof ApiError ? e : new ApiError(0, 'UNKNOWN', 'Something went wrong.'));
