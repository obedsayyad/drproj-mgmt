/**
 * Operation progress as spec/events.schema.json envelopes (docs/06). The durable row in
 * ops.operation_events is the source; `sequence` is the SSE id.
 *
 * Dispatch outcomes use the schema's lifecycle types, with the detail in `step`:
 *   accepted by the API            -> operation.started   (step accepted)
 *   handed to StackStorm           -> operation.started   (step dispatched)
 *   StackStorm not configured      -> operation.waiting   (step awaiting_automation)
 *   StackStorm launch failed       -> operation.failed    (step dispatch_failed)
 * The operation's own status stays `queued` through all of these; only automation advances it.
 */
export const SERVICE_ID = 'operations';

export interface EventRow {
  event_id: string;
  sequence: string | number;
  event_type: string;
  status: string | null;
  step: string | null;
  message: string | null;
  source: string;
  occurred_at: string;
  operation_id: string;
  policy_version: number;
}

export interface EventEnvelope {
  event_id: string;
  event_type: string;
  source: string;
  service_id: string;
  observed_at: string;
  policy_version: number;
  measurements: Record<string, string | null>;
  operation_id: string;
  severity: 'info' | 'warning' | 'critical';
  sequence: number;
}

export function toEventEnvelope(row: EventRow): EventEnvelope {
  return {
    event_id: row.event_id,
    event_type: row.event_type,
    source: row.source,
    service_id: SERVICE_ID,
    observed_at: row.occurred_at,
    policy_version: row.policy_version,
    measurements: { status: row.status, step: row.step, message: row.message },
    operation_id: row.operation_id,
    severity: row.event_type === 'operation.failed' ? 'warning' : 'info',
    sequence: Number(row.sequence),
  };
}
