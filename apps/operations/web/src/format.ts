const rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
export function ago(iso: string | null | undefined): string {
  if (!iso) return 'never';
  const s = Math.round((new Date(iso).getTime() - Date.now()) / 1000);
  const a = Math.abs(s);
  if (a < 60) return rtf.format(s, 'second');
  if (a < 3600) return rtf.format(Math.round(s / 60), 'minute');
  if (a < 86400) return rtf.format(Math.round(s / 3600), 'hour');
  return rtf.format(Math.round(s / 86400), 'day');
}
export const when = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString('en-IN', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';
export const human = (s: string) => s.replaceAll('_', ' ').replace(/^./, (c) => c.toUpperCase());
export const mb = (b: number | null) => (b === null ? '—' : `${(b / 1024 / 1024).toFixed(1)} MB`);

export const KIND_LABELS: Record<string, string> = {
  activate_standby: 'Activate warm standby',
  scale_out: 'Add one cloud VM',
  scale_in: 'Remove one cloud VM',
  standby_repair: 'Repair warm standby',
  reconcile: 'Reconcile with providers',
  failback: 'Return traffic to primary',
  apply_policy: 'Apply policy',
  create_backup: 'Back up app data',
  verify_backup: 'Verify backup',
  restore_application: 'Restore app data',
  start_load_test: 'Start load test',
  stop_load_test: 'Stop load test',
  primary_failure: 'Primary failure response',
  provision_and_deploy: 'Provision and deploy',
};
export const kindLabel = (k: string) => KIND_LABELS[k] ?? human(k);
