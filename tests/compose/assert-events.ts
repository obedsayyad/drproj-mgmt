// Validates operation_events rows (JSON lines on stdin, as selected by t08-t09-local.sh) against
// docs/spec/events.schema.json after the SAME row -> envelope mapping the ops API uses for its SSE stream.
// usage: pnpm exec tsx tests/compose/assert-events.ts   (prints "ok <n>" or the first violations; exit 1 on any)
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { toEventEnvelope, type EventRow } from '../../apps/operations/src/events.ts';

const schema = JSON.parse(readFileSync(fileURLToPath(new URL('../../docs/spec/events.schema.json', import.meta.url)), 'utf8'));
const ajv = new Ajv2020.default({ allErrors: true, strict: false });
(addFormats as unknown as { default: (a: unknown) => void }).default(ajv);
const validate = ajv.compile(schema);

const rows = readFileSync(0, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l) as EventRow);
const bad: string[] = [];
for (const row of rows) {
  const env = toEventEnvelope(row);
  if (!validate(env)) bad.push(`${row.event_type} seq ${row.sequence}: ${ajv.errorsText(validate.errors)}`);
}
if (rows.length === 0) bad.push('no events to validate');
if (bad.length) {
  console.log(bad.slice(0, 5).join('\n'));
  process.exit(1);
}
console.log(`ok ${rows.length}`);
