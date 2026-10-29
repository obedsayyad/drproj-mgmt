// Bounded k6 scenarios for the relief app (docs/10 "Bounded k6 tests"). One script, one scenario per run, selected with -e SCENARIO.
// Every scenario is a constant-arrival-rate executor so the offered load stays fixed while the platform scales (achieved rate and
// dropped iterations are in the summary: a saturated generator must not look like server improvement).
// The caps below mirror policy demo.* and are enforced again by k6ctl on the testing host; this script refuses to start above them.
import http from 'k6/http';
import { check } from 'k6';

const TARGET = __ENV.TARGET || 'https://app.cproj.obed.one';
const SCENARIO = __ENV.SCENARIO || 'smoke';
const RATE = parseInt(__ENV.RATE || '10', 10);
const DURATION = parseInt(__ENV.DURATION || '60', 10);
const WORK_MS = parseInt(__ENV.WORK_MS || '100', 10);
const RUN_ID = __ENV.RUN_ID || 'adhoc';
const RESULTS_DIR = __ENV.RESULTS_DIR || '/results';
const MAX_RATE = 200, MAX_DURATION = 600, MAX_WORK_MS = 250;

// smoke / baseline / recovery: light requests (the health endpoint plus a short workload); compute_pressure / sustained_surge: every
// request carries the CPU workload. mix = share of requests that run the workload.
const MIX = { smoke: 0.2, baseline: 0.5, compute_pressure: 1, sustained_surge: 1, recovery: 0.2 };

if (!(SCENARIO in MIX) || !(RATE >= 1 && RATE <= MAX_RATE) || !(DURATION >= 1 && DURATION <= MAX_DURATION) || !(WORK_MS >= 1 && WORK_MS <= MAX_WORK_MS)) {
  throw new Error(`bounds: scenario=${SCENARIO} rate=${RATE} (1..${MAX_RATE}) duration=${DURATION} (1..${MAX_DURATION}) work_ms=${WORK_MS} (1..${MAX_WORK_MS})`);
}

export const options = {
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  scenarios: {
    [SCENARIO]: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: `${DURATION}s`,
      // Sized for the latency we expect under pressure (a few seconds), bounded so the generator cannot be driven out of memory.
      preAllocatedVUs: Math.min(RATE * 2 + 10, 300),
      maxVUs: Math.min(RATE * 6 + 50, 800),
      gracefulStop: '15s',
    },
  },
  userAgent: `drproj-k6/${RUN_ID}`,
  noConnectionReuse: false,
};

const JSON_HEADERS = { headers: { 'Content-Type': 'application/json' } };

export default function () {
  if (Math.random() < MIX[SCENARIO]) {
    const r = http.post(`${TARGET}/api/demo/workload`, JSON.stringify({ work_ms: WORK_MS }), { ...JSON_HEADERS, tags: { kind: 'workload' } });
    check(r, { 'workload 200': (x) => x.status === 200 });
  } else {
    const r = http.get(`${TARGET}/health/live`, { tags: { kind: 'health' } });
    check(r, { 'live 200': (x) => x.status === 200 });
  }
}

// result.json is what the pack's parser reads (ops.test_runs.summary); summary.json is the full k6 export kept as evidence.
export function handleSummary(data) {
  const m = data.metrics;
  const v = (name, key) => (m[name] && m[name].values && m[name].values[key] !== undefined ? m[name].values[key] : null);
  const result = {
    run_id: RUN_ID, scenario: SCENARIO, rate_rps: RATE, duration_seconds: DURATION, work_ms: WORK_MS, target: TARGET,
    p95_ms: v('http_req_duration', 'p(95)'),
    avg_ms: v('http_req_duration', 'avg'),
    max_ms: v('http_req_duration', 'max'),
    error_rate: v('http_req_failed', 'rate'),
    requests: v('http_reqs', 'count'),
    achieved_rps: v('http_reqs', 'rate'),
    dropped_iterations: v('dropped_iterations', 'count') || 0,
    vus_max: v('vus_max', 'max'),
  };
  return {
    [`${RESULTS_DIR}/${RUN_ID}/result.json`]: JSON.stringify(result, null, 2),
    [`${RESULTS_DIR}/${RUN_ID}/summary.json`]: JSON.stringify(data, null, 2),
  };
}
