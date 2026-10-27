#!/bin/bash
# P3.2 live smoke (T16/T30 slices, st2 auth, alert recording) plus a T18 PREVIEW, run LOCALLY under Docker Desktop.
# This is NOT the management VM: Postgres is a throwaway container, StackStorm 3.9.0 images are amd64-only and run
# emulated on Apple silicon, Caddy uses its internal CA on loopback, nothing is deployed. Output is recorded in
# tests/compose/t16-t30.local.out.
#
#   tests/compose/t16-t30-local.sh
# Needs: docker, pnpm, node, python3, openssl. Set COMPOSE_BIN if `docker compose` is unavailable.
set -u -o pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
MGMT=$(cd "$HERE/../.." && pwd)
APP=$(cd "$MGMT/../drproj-app" && pwd)
OUT=${T16_OUT:-$HERE/t16-t30.local.out}
PORT=${T16_HTTPS_PORT:-18444}
PGPORT=${T16_PG_PORT:-55434}
PROJECT=drproj-t16
NET=${PROJECT}_internal
WAIT=${T16_WAIT_SECONDS:-900}

if docker compose version >/dev/null 2>&1; then DC=(docker compose); else DC=(${COMPOSE_BIN:-docker-compose}); fi

SAMPLER=0
# Under the repo (gitignored, 0700, removed on exit): Docker Desktop file sharing covers /Users but not /var/folders.
TMP=$(mktemp -d "$MGMT/.t16.XXXXXX"); chmod 700 "$TMP"; mkdir -m 700 "$TMP/secrets"
PASS=0; FAILN=0
exec > >(tee "$OUT") 2>&1

say() { printf '%s\n' "$*"; }
pass() { PASS=$((PASS + 1)); say "PASS $*"; }
fail() { FAILN=$((FAILN + 1)); say "FAIL $*"; }
expect() { if [ "$2" = "$3" ]; then pass "$1 ($2)"; else fail "$1 (got $2, want $3)"; fi; }
redact() { sed -e "s/${PG_PW:-@@}/***/g" -e "s/${ST2_PW:-@@}/***/g" -e "s/${MONGO_PW:-@@}/***/g" -e "s/${RMQ_PW:-@@}/***/g"; }

export MGMT_SECRETS_DIR="$TMP/secrets"
cd "$MGMT/deploy/compose/mgmt" || exit 1
C() { "${DC[@]}" -p "$PROJECT" --env-file "$TMP/mgmt.env" -f compose.yaml -f compose.local.yaml --profile local --profile init --profile restore-test "$@"; }
cid() { C ps -q "$1"; }
cleanup() {
  if [ -n "${T16_KEEP:-}" ]; then say "--- kept stack (T16_KEEP set); project $PROJECT, secrets in $TMP"; return; fi
  say "--- cleanup"
  [ "${SAMPLER:-0}" -gt 0 ] && kill "$SAMPLER" 2>/dev/null
  C down -v --remove-orphans >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT

# curl from inside the Compose network (nothing is published except Caddy and Postgres on loopback).
NC() { docker run --rm --network "$NET" curlimages/curl:8.11.1 -s --max-time 20 "$@"; }
# st2 CLI inside the actionrunner container (the st2client lives in the st2 venv).
ST2() { docker exec -e ST2_API_URL=http://st2api:9101 -e ST2_AUTH_URL=http://st2auth:9100 ${ST2_AUTH_TOKEN:+-e ST2_AUTH_TOKEN=$ST2_AUTH_TOKEN} "$(cid st2actionrunner)" /opt/stackstorm/st2/bin/st2 "$@"; }
SQL() { docker exec -i "$(cid localpg)" psql -U postgres -v ON_ERROR_STOP=1 -qAt "$@"; }
upwait() { C up -d --wait --wait-timeout "$WAIT" "$@" >"$TMP/up.log" 2>&1 || { redact <"$TMP/up.log" | tail -12 | sed 's/^/    up: /'; return 1; }; }

say "T16/T30 LOCAL RUN - Docker Desktop on a developer Mac ($(uname -m), docker VM $(docker info --format '{{.NCPU}} CPU / {{.MemTotal}} bytes')), NOT the management VM"
say "StackStorm images run as linux/amd64 (emulated on arm64); timings and memory are NOT representative of the 2 vCPU / 4 GB VM"
say "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ---- secrets for this run (random, never printed) ----
rnd() { openssl rand -hex "$1"; }
OPS_PW=$(rnd 16); PG_PW=$(rnd 12); ST2_PW=$(rnd 12); MONGO_PW=$(rnd 12); RMQ_PW=$(rnd 12); GRAF_PW=$(rnd 12)
CADDY_TAG=$(sed -n 's/.*CADDY_TAG:-\([^}]*\)}.*/\1/p' compose.yaml | head -1)
HASH=$(docker run --rm "caddy:$CADDY_TAG" caddy hash-password --plaintext "$OPS_PW")
umask 077
cat >"$TMP/mgmt.env" <<ENV
SUPABASE_OPS_URL=postgres://ops_t16:${PG_PW}@localpg:5432/postgres
OPS_OPERATOR_PASSWORD=${OPS_PW}
OPS_BASIC_AUTH_HASH='${HASH}'
MONGO_ROOT_USER=mongoroot
MONGO_ROOT_PASSWORD=${MONGO_PW}
RABBITMQ_USER=rmq
RABBITMQ_PASSWORD=${RMQ_PW}
RESTORE_TEST_PG_PASSWORD=${PG_PW}
LOCAL_PG_PASSWORD=${PG_PW}
ACME_EMAIL=
ST2_API_URL=http://st2api:9101
T31_HTTPS_PORT=${PORT}
T31_PG_PORT=${PGPORT}
ENV
printf '[database]\nusername = mongoroot\npassword = %s\n\n[messaging]\nurl = amqp://rmq:%s@rabbitmq:5672/\n' "$MONGO_PW" "$RMQ_PW" >"$TMP/secrets/st2.user.conf"
printf 'supabase_ops_url: "postgres://ops_t16:%s@localpg:5432/postgres"\n' "$PG_PW" >"$TMP/secrets/cdr-pack-config.yaml"
docker run --rm httpd:2.4-alpine htpasswd -nbB st2admin "$ST2_PW" >"$TMP/secrets/st2-htpasswd"
printf '%s' "$GRAF_PW" >"$TMP/secrets/grafana_admin_password"
: >"$TMP/secrets/st2_api_key"
chmod 600 "$TMP/secrets/"* "$TMP/mgmt.env"; umask 022
"$MGMT/scripts/check-perms.sh" "$TMP/mgmt.env" >/dev/null && pass "secrets: .env is 0600 in a 0700 dir" || fail "check-perms"

say "--- 1. Postgres, migrations (incl. ops/0008), seed policy"
(cd "$MGMT" && docker build -q -f apps/operations/Dockerfile -t drproj-operations:local . >/dev/null) && pass "ops image builds (local tag, not pushed)" || fail "ops image build"
upwait localpg && pass "localpg healthy" || { fail "localpg did not start"; exit 1; }
SUPABASE_MIGRATION_URL="postgres://postgres:${PG_PW}@127.0.0.1:${PGPORT}/postgres" pnpm --dir "$APP" -s migrate 2>&1 | redact | tail -3 | sed 's/^/    /'
SQL -c "CREATE ROLE ops_t16 LOGIN PASSWORD '${PG_PW}' IN ROLE dr_ops" -c "CREATE ROLE app_t16 LOGIN PASSWORD '${PG_PW}' IN ROLE dr_app_runtime" && pass "login roles created (dr_ops, dr_app_runtime)" || fail "login roles"
(cd "$MGMT" && SUPABASE_OPS_URL="postgres://ops_t16:${PG_PW}@127.0.0.1:${PGPORT}/postgres" pnpm -s seed:policy 2>&1 | redact | tail -1 | sed 's/^/    /')
expect "processed_events has source+event_name columns" "$(SQL -c "SELECT count(*) FROM information_schema.columns WHERE table_schema='ops' AND table_name='processed_events' AND column_name IN ('source','event_name')")" 2

say "--- 2. StackStorm stores and services (emulated amd64: slow)"
upwait mongo rabbitmq redis && pass "mongo, rabbitmq, redis healthy" || { fail "native stores"; C logs --tail 15 mongo rabbitmq 2>&1 | redact | sed 's/^/    /'; exit 1; }
# On an empty MongoDB every st2 service registers the runner types at once and all but one fail with a duplicate
# key error (then restart). Starting st2api first and waiting for it avoids the churn on first boot.
upwait st2api && pass "st2api healthy (runner types registered)" || fail "st2api"
upwait st2api st2auth st2stream st2actionrunner st2scheduler st2workflowengine st2notifier st2garbagecollector st2rulesengine st2timersengine && pass "st2 services healthy" || { fail "st2 services not healthy"; C ps 2>&1 | sed 's/^/    /'; C logs --tail 20 st2api st2auth 2>&1 | redact | sed 's/^/    /'; exit 1; }
C run --rm st2-register >"$TMP/register.log" 2>&1 && pass "st2-register --register-all (pack cdr, rules, config)" || { fail "st2-register"; redact <"$TMP/register.log" | tail -15 | sed 's/^/    /'; }

say "--- 3. st2 authentication rejects unauthenticated requests"
expect "GET /v1/actions without credentials" "$(NC -o /dev/null -w '%{http_code}' http://st2api:9101/v1/actions)" 401
expect "POST /v1/webhooks/cdr_alert without St2-Api-Key" "$(NC -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' -d '{}' http://st2api:9101/v1/webhooks/cdr_alert)" 401
expect "POST /v1/webhooks/cdr_alert with a wrong St2-Api-Key" "$(NC -o /dev/null -w '%{http_code}' -X POST -H 'St2-Api-Key: not-a-key' -H 'content-type: application/json' -d '{}' http://st2api:9101/v1/webhooks/cdr_alert)" 401
expect "st2auth with a wrong password" "$(NC -o /dev/null -w '%{http_code}' -X POST -u "st2admin:wrong-password" http://st2auth:9100/tokens)" 401
expect "st2auth with the st2admin password" "$(NC -o /dev/null -w '%{http_code}' -X POST -u "st2admin:${ST2_PW}" http://st2auth:9100/tokens)" 201
ST2_AUTH_TOKEN=$(ST2 auth st2admin -p "$ST2_PW" -t 2>/dev/null | tail -1)
[ -n "$ST2_AUTH_TOKEN" ] && pass "st2 token obtained (not printed)" || fail "st2 token"
expect "GET /v1/actions with a token" "$(NC -o /dev/null -w '%{http_code}' -H "X-Auth-Token: $ST2_AUTH_TOKEN" http://st2api:9101/v1/actions)" 200
ST2 pack get cdr >/dev/null 2>&1 && pass "pack cdr registered" || fail "pack cdr not registered"
for ref in cdr.keepalive cdr.record_alert; do ST2 action get "$ref" >/dev/null 2>&1 && pass "action $ref registered" || fail "action $ref missing"; done
ST2 rule get cdr.cdr_alert >/dev/null 2>&1 && pass "rule cdr.cdr_alert registered" || fail "rule cdr.cdr_alert missing"
# The documented route is `st2 run packs.setup_virtualenv packs=cdr`. In this local emulated run that action took the
# actionrunner container down mid-pip (execution stays "running", container restarts, no OOM flag), so its outcome is
# reported as observed and the venv is completed with the pack's own requirements.txt, then verified by importing it.
if ST2 run packs.setup_virtualenv packs=cdr >"$TMP/venv.log" 2>&1; then say "    observed: packs.setup_virtualenv succeeded"; else say "    observed: packs.setup_virtualenv did not complete (runner restarts so far: $(docker inspect -f '{{.RestartCount}}' "$(cid st2actionrunner)"))"; fi
upwait st2actionrunner && pass "st2actionrunner healthy after the virtualenv step" || fail "st2actionrunner after virtualenv step"
docker exec "$(cid st2actionrunner)" bash -c 'test -x /opt/stackstorm/virtualenvs/cdr/bin/pip || /opt/stackstorm/st2/bin/python -m virtualenv --always-copy /opt/stackstorm/virtualenvs/cdr >/dev/null; /opt/stackstorm/virtualenvs/cdr/bin/pip install -q -r /opt/stackstorm/packs/cdr/requirements.txt >/dev/null 2>&1; /opt/stackstorm/virtualenvs/cdr/bin/python -c "import psycopg"' && pass "pack virtualenv has psycopg (pip install -r requirements.txt)" || fail "pack virtualenv psycopg"
KEY=$(ST2 apikey create -k -m '{"used_by":"alertmanager"}' 2>"$TMP/key.err" | tail -1)
if [ -n "$KEY" ]; then printf '%s' "$KEY" >"$TMP/secrets/st2_api_key"; pass "St2-Api-Key created and written to the secret file (not printed)"; else fail "apikey create"; redact <"$TMP/key.err" | tail -5 | sed 's/^/    /'; fi
sed -i.bak "s|^ST2_API_URL=.*|&\nST2_API_KEY=${KEY}|" "$TMP/mgmt.env"; rm -f "$TMP/mgmt.env.bak"

say "--- 4. T30: keep-alive action run manually"
before=$(SQL -c "SELECT count(*) FROM ops.keepalive_runs WHERE succeeded")
ST2 run cdr.keepalive >"$TMP/ka.log" 2>&1; ka=$?
after=$(SQL -c "SELECT count(*) FROM ops.keepalive_runs WHERE succeeded")
[ "$ka" -eq 0 ] && pass "st2 run cdr.keepalive succeeded" || { fail "st2 run cdr.keepalive (exit $ka)"; redact <"$TMP/ka.log" | tail -12 | sed 's/^/    /'; }
expect "ops.keepalive_runs gained a succeeded row" "$((after - before))" 1
say "    latest row: $(SQL -c "SELECT succeeded, source, coalesce(detail,'-'), to_char(finished_at at time zone 'utc','HH24:MI:SS\"Z\"') FROM ops.keepalive_runs ORDER BY finished_at DESC LIMIT 1")"

say "--- 5. monitoring stack up (prometheus, alertmanager, grafana, ops, caddy)"
upwait prometheus alertmanager grafana blackbox ops caddy && pass "monitoring and ops healthy" || { fail "monitoring stack"; C ps 2>&1 | sed 's/^/    /'; C logs --tail 10 prometheus alertmanager grafana ops 2>&1 | redact | sed 's/^/    /'; }

say "--- 6. T16/T30: alert delivery to st2 records ops.processed_events"
am=$(cid alertmanager)
docker exec "$am" amtool alert add CdrSmokeTest severity=warning policy_version=1 --annotation=summary="P3.2 smoke test alert" --alertmanager.url=http://localhost:9093 >/dev/null 2>&1 && pass "amtool alert add CdrSmokeTest" || fail "amtool alert add"
got=0
for i in $(seq 1 45); do
  got=$(SQL -c "SELECT count(*) FROM ops.processed_events WHERE source='alertmanager' AND event_name='CdrSmokeTest'")
  [ "$got" -ge 1 ] && break; sleep 2
done
expect "processed_events row for CdrSmokeTest (source=alertmanager)" "$got" 1
say "    row: $(SQL -c "SELECT policy_version, source, event_name, (last_seen_at >= first_seen_at) FROM ops.processed_events WHERE event_name='CdrSmokeTest'")"
say "    st2 executions of cdr.record_alert: $(ST2 execution list --action cdr.record_alert -n 3 2>/dev/null | grep -c 'succeeded') succeeded in the last 3"
nat=$(SQL -c "SELECT count(*) FROM ops.processed_events WHERE event_name='CdrMembershipAbsent'")
say "    observed (not asserted): CdrMembershipAbsent reached st2 on its own with an empty inventory: ${nat} row(s); active alerts now: $(docker exec "$am" amtool alert query --alertmanager.url=http://localhost:9093 -o simple 2>/dev/null | awk 'NR>1{print $1}' | sort -u | tr '\n' ' ')"
ST2 execution list --action cdr.record_alert -n 1 2>/dev/null | grep -qi 'succeeded' && pass "cdr.record_alert execution succeeded" || fail "cdr.record_alert execution"
rlog=$(docker exec "$am" wget -q -O - http://localhost:9093/metrics 2>/dev/null | grep -E '^alertmanager_notifications_(total|failed_total)\{integration="webhook"' | tr '\n' ' ')
say "    alertmanager webhook counters: ${rlog}"

say "--- 7. /api/overview Prometheus probe against the live stack"
state=""
for i in $(seq 1 24); do
  ov=$(NC http://ops:8080/api/overview)
  state=$(printf '%s' "$ov" | python3 -c "import sys,json; i=json.load(sys.stdin)['integrations']; print(i['prometheus']['state'], i['stackstorm']['state'], i['haproxy'].get('detail'))" 2>/dev/null)
  case "$state" in "fresh fresh"*) break ;; esac; sleep 5
done
say "    integrations (prometheus stackstorm haproxy-detail): ${state}"
case "$state" in "fresh fresh not probed in this phase") pass "prometheus fresh, stackstorm fresh, haproxy not probed" ;; *) fail "overview integrations: ${state}" ;; esac
tgt=$(docker exec "$(cid prometheus)" wget -q -O - 'http://localhost:9090/api/v1/targets?state=active' | python3 -c "import sys,json; print(' '.join(sorted(f\"{t['scrapePool']}={t['health']}\" for t in json.load(sys.stdin)['data']['activeTargets'])))")
say "    prometheus targets: ${tgt}"
C stop alertmanager >/dev/null 2>&1; sleep 40
stale=$(NC http://ops:8080/api/overview | python3 -c "import sys,json; p=json.load(sys.stdin)['integrations']['prometheus']; print(p['state'], '-', p.get('detail'))")
say "    after stopping alertmanager for 40 s: ${stale}"
case "$stale" in stale*) pass "a down expected target turns the Prometheus integration stale" ;; *) fail "stale detection: ${stale}" ;; esac
C start alertmanager >/dev/null 2>&1

say "--- 7b. database outage: ops stays live, reports not-ready, does not restart, recovers"
OPSID=$(cid ops)
rc_before=$(docker inspect -f '{{.RestartCount}}' "$OPSID")
C stop localpg >/dev/null 2>&1 && pass "localpg stopped" || fail "stop localpg"
sleep 3
expect "/health/live during the outage" "$(NC -o /dev/null -w '%{http_code}' http://ops:8080/health/live)" 200
resp=$(NC -w '\n%{http_code}' http://ops:8080/health/ready)   # NC runs in a curl container: read the body from stdout, not -o
expect "/health/ready during the outage" "${resp##*$'\n'}" 503
case "$resp" in *DEPENDENCY_UNAVAILABLE*) pass "/health/ready body carries DEPENDENCY_UNAVAILABLE" ;; *) fail "/health/ready body: ${resp:0:200}" ;; esac
sleep 35   # longer than two 15 s container healthchecks: /health/live must not depend on the database
expect "ops RestartCount unchanged after the outage" "$(docker inspect -f '{{.RestartCount}}' "$OPSID")" "$rc_before"
expect "/health/live still 200 after 35 s" "$(NC -o /dev/null -w '%{http_code}' http://ops:8080/health/live)" 200
upwait localpg && pass "localpg restarted healthy" || fail "localpg restart"
rdy=0
for i in $(seq 1 20); do rdy=$(NC -o /dev/null -w '%{http_code}' http://ops:8080/health/ready); [ "$rdy" = 200 ] && break; sleep 3; done
expect "/health/ready after the database returns" "$rdy" 200
expect "ops RestartCount unchanged after recovery" "$(docker inspect -f '{{.RestartCount}}' "$OPSID")" "$rc_before"

say "--- 8. Grafana provisioning"
GCODE=$(NC -o "$TMP/g.json" -w '%{http_code}' -u "admin:${GRAF_PW}" "http://grafana:3000/api/search?tag=cdr" || true)
titles=$(NC -u "admin:${GRAF_PW}" "http://grafana:3000/api/search?tag=cdr" | python3 -c "import sys,json; print(','.join(sorted(d['title'] for d in json.load(sys.stdin))))" 2>/dev/null)
say "    dashboards: ${titles}"
expect "provisioned dashboards" "$titles" "CDR Fleet,CDR Management VM,CDR Relief HTTP,CDR Supabase"
expect "grafana anonymous access is off" "$(NC -o /dev/null -w '%{http_code}' http://grafana:3000/api/search)" 401
dsq=$(NC -u "admin:${GRAF_PW}" "http://grafana:3000/api/datasources/proxy/uid/prometheus/api/v1/query?query=up%7Bjob%3D%22ops%22%7D" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['data']['result']))" 2>/dev/null)
expect "datasource proxy returns the ops target" "$dsq" 1

say "--- 9. T18 PREVIEW (local Docker Desktop, NOT the management VM): stack memory under one pg_restore of the seed dump"
SUPABASE_APP_URL="postgres://app_t16:${PG_PW}@127.0.0.1:${PGPORT}/postgres" pnpm --dir "$APP/apps/relief" -s seed 2>&1 | redact | tail -2 | sed 's/^/    /'
rows=$(SQL -c "SELECT count(*) FROM app.relief_requests")
[ "$rows" -gt 0 ] && pass "seed data present in the app schema (${rows} relief_requests)" || fail "no seed data"
docker exec "$(cid localpg)" pg_dump -U postgres -Fc -n app postgres >"$TMP/seed.dump" && pass "pg_dump -Fc -n app ($(wc -c <"$TMP/seed.dump" | tr -d ' ') bytes)" || fail "pg_dump"
sample() { while :; do docker stats --no-stream --format '{{.Name}}|{{.MemUsage}}|{{.CPUPerc}}' ; sleep 1; done >>"$1" 2>/dev/null; }
sample "$TMP/base.stats" & SAMPLER=$!
sleep 12; kill $SAMPLER 2>/dev/null; wait $SAMPLER 2>/dev/null
upwait restore-test-pg && pass "restore-test-pg healthy" || fail "restore-test-pg"
sample "$TMP/restore.stats" & SAMPLER=$!
docker exec -i "$(cid restore-test-pg)" pg_restore -U postgres -d postgres --no-owner --no-privileges --exit-on-error <"$TMP/seed.dump" && pass "pg_restore of the seed dump" || fail "pg_restore"
sleep 4; kill $SAMPLER 2>/dev/null; wait $SAMPLER 2>/dev/null; SAMPLER=0
restored=$(docker exec "$(cid restore-test-pg)" psql -U postgres -qAt -c "SELECT count(*) FROM app.relief_requests")
expect "restored row count equals source" "$restored" "$rows"
python3 - "$TMP/base.stats" "$TMP/restore.stats" "$MGMT/deploy/compose/mgmt/compose.yaml" <<'PY'
import re, sys
def mib(s):
    m = re.match(r'([\d.]+)\s*([KMG]i?B|B)', s.strip())
    v, u = float(m.group(1)), m.group(2)
    return v * {'B': 1/1048576, 'KiB': 1/1024, 'MiB': 1, 'GiB': 1024, 'kB': 1/1024, 'MB': 0.953674, 'GB': 976.56}.get(u, 1)
def peaks(path):
    d = {}
    for line in open(path):
        p = line.rstrip('\n').split('|')
        if len(p) != 3: continue
        used, lim = (x.strip() for x in p[1].split('/'))
        name = re.sub(r'^drproj-t16-|-1$', '', p[0])
        if name == 'localpg': continue  # test scaffolding, not part of the management stack
        e = d.setdefault(name, [0, 0]); e[0] = max(e[0], mib(used)); e[1] = mib(lim)
    return d
base, rest = peaks(sys.argv[1]), peaks(sys.argv[2])
print(f"    {'container':22} {'idle peak':>10} {'restore peak':>13} {'limit':>9}   (MiB)")
tb = tr = tl = 0
for n in sorted(rest):
    b = base.get(n, [0, 0])[0]; r = max(rest[n][0], b); l = rest[n][1]
    tb += b; tr += r; tl += l
    print(f"    {n:22} {b:10.1f} {r:13.1f} {l:9.0f}")
print(f"    {'SUM of per-container peaks':22} {tb:10.1f} {tr:13.1f} {tl:9.0f}")
print("    note: sum of peaks over-states simultaneous use; mem_limit values are the ceilings in compose.yaml; the seed dump is tiny, so this is a preview of the harness, not the T18 result.")
PY

say "--- summary"
say "passed=$PASS failed=$FAILN"
say "images: $(docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Image}}' | sort -u | tr '\n' ' ')"
[ "$FAILN" -eq 0 ]
