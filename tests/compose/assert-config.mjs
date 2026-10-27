// Structural hardening checks on `docker compose config --format json` (stdin).
// usage: node assert-config.mjs <role: mgmt|testing|app|lb>
import { readFileSync } from 'node:fs';

const role = process.argv[2];
const cfg = JSON.parse(readFileSync(0, 'utf8'));
const services = cfg.services ?? {};
const failures = [];
const fail = (svc, msg) => failures.push(`${svc}: ${msg}`);
const FIRST_PARTY = /^drproj-(relief|operations|runner):local$/;

// Only Caddy (mgmt) and the edge/app/lb listeners below may publish host ports.
const PUBLISHERS = { mgmt: ['caddy'], testing: [], app: ['relief', 'node_exporter'], lb: ['haproxy'] }[role];
const MUST_BE_INTERNAL = ['prometheus', 'alertmanager', 'mongo', 'rabbitmq', 'redis', 'ops', 'grafana', 'blackbox', 'restore-test-pg', 'localpg'];

for (const [name, s] of Object.entries(services)) {
  if (!(Number(s.mem_limit) > 0)) fail(name, 'no mem_limit');
  if (!s.healthcheck?.test) fail(name, 'no healthcheck');
  const image = s.image ?? '';
  const tag = image.includes('@sha256:') ? 'digest' : image.split('/').pop().split(':')[1];
  if (!tag || tag === 'latest') fail(name, `image not pinned (${image})`);
  if (/:local$/.test(image) && !FIRST_PARTY.test(image)) fail(name, `unexpected :local image ${image}`);
  const published = (s.ports ?? []).length > 0;
  if (published && !PUBLISHERS.includes(name) && name !== 'localpg') fail(name, `publishes host ports`);
  if (MUST_BE_INTERNAL.includes(name) && published && name !== 'localpg') fail(name, 'must have no host ports');
  if (s.restart !== 'unless-stopped' && !(s.profiles ?? []).length) fail(name, `restart policy ${s.restart}`);
}
for (const n of PUBLISHERS) if (services[n] && !(services[n].ports ?? []).length) fail(n, 'expected to publish ports');

if (role === 'mgmt') {
  const runners = Object.entries(services).filter(([, s]) => /st2actionrunner/.test(s.image ?? '') && !(s.profiles ?? []).length);
  if (runners.length !== 1) fail('st2actionrunner', `expected exactly 1 always-on actionrunner, found ${runners.length}`);
  if (runners[0] && (runners[0][1].deploy?.replicas ?? 1) !== 1) fail('st2actionrunner', 'replicas != 1');
  if (!(services['restore-test-pg']?.profiles ?? []).includes('restore-test')) fail('restore-test-pg', 'must sit behind the restore-test profile');
  const g = services.grafana?.environment ?? {};
  if (g.GF_AUTH_ANONYMOUS_ENABLED !== 'false') fail('grafana', 'anonymous access not disabled');
  if (g.GF_USERS_ALLOW_SIGN_UP !== 'false') fail('grafana', 'sign-up not disabled');
  if (!g.GF_SECURITY_ADMIN_PASSWORD__FILE) fail('grafana', 'admin password is not read from a file');
  if (services.ops?.environment?.OPS_TRUST_PROXY_AUTH !== 'false') fail('ops', 'must not trust proxy auth alone');
  if (!(services.prometheus?.command ?? []).includes('--storage.tsdb.retention.time=3d')) fail('prometheus', 'retention is not 3d');
  const conf = readFileSync(new URL('../../deploy/compose/mgmt/stackstorm/st2.docker.conf', import.meta.url), 'utf8');
  if (!/^\[auth\][^[]*enable = True/m.test(conf)) fail('st2', 'auth is not enabled in st2.docker.conf');
  if (!/redis:\/\/redis/.test(conf)) fail('st2', 'coordination is not Redis');
}
if (role === 'app') {
  const ne = (services.node_exporter?.command ?? []).join(' ');
  if (!ne.includes('--web.config.file=')) fail('node_exporter', 'no --web.config.file (TLS + basic auth)');
}

if (failures.length) {
  console.log(`FAIL ${role}`);
  for (const f of failures) console.log(`  - ${f}`);
  process.exit(1);
}
console.log(`ok   ${role}: ${Object.keys(services).length} services (mem_limit, healthcheck, pinned tag, ports, profiles)`);
