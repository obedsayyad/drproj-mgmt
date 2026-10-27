#!/bin/bash
# Shared bring-up and helpers of the local StackStorm simulation runs (t08-t09-local.sh, t10-t22-local.sh).
# Sourced, not executed. The caller sets HERE, MGMT, APP, OUT, PGPORT, PROJECT (and optionally RUN_LABEL) first.
# Everything here is the LOCAL SIMULATION: a throwaway Postgres container and emulated StackStorm containers, no cloud.
set -u -o pipefail

WAIT=${T08_WAIT_SECONDS:-900}
SIM=/opt/stackstorm/cdr-sim

if docker compose version >/dev/null 2>&1; then DC=(docker compose); else DC=(${COMPOSE_BIN:-docker-compose}); fi

TMP=$(mktemp -d "$MGMT/.t08.XXXXXX"); chmod 700 "$TMP"; mkdir -m 700 "$TMP/secrets"
PASS=0; FAILN=0
exec > >(tee "$OUT") 2>&1

say() { printf '%s\n' "$*"; }
pass() { PASS=$((PASS + 1)); say "PASS $*"; }
EVID=""; EVN=0; CAPTURING=0
# Per-step durations go to <evidence dir>/durations.log on EVERY run (pass or fail): "<utc> <seconds>s <result> <label>".
STEP_LOG=""
step_log() {  # $1 label  $2 start (SECONDS)  $3 result
  [ -n "$STEP_LOG" ] || { [ -n "$EVID" ] || EVID="$HERE/evidence/$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$EVID"; STEP_LOG="$EVID/durations.log"; }
  printf '%s %ss %s %s\n' "$(date -u +%H:%M:%S)" "$((SECONDS - $2))" "$3" "$1" >>"$STEP_LOG"
}
fail() { FAILN=$((FAILN + 1)); say "FAIL $*"; [ "$CAPTURING" = 0 ] && [ "$EVN" = 0 ] && capture_evidence "first-fail"; return 0; }
expect() { if [ "$2" = "$3" ]; then pass "$1 ($2)"; else fail "$1 (got $2, want $3)"; fi; }
redact() { sed -e "s/${PG_PW:-@@}/***/g" -e "s/${ST2_PW:-@@}/***/g" -e "s/${MONGO_PW:-@@}/***/g" -e "s/${RMQ_PW:-@@}/***/g"; }

export MGMT_SECRETS_DIR="$TMP/secrets"
cd "$MGMT/deploy/compose/mgmt" || exit 1
C() { "${DC[@]}" -p "$PROJECT" --env-file "$TMP/mgmt.env" -f compose.yaml -f compose.local.yaml --profile local --profile init "$@"; }
cid() { C ps -q "$1"; }
cleanup() {
  if [ -n "${T08_KEEP:-}" ]; then say "--- kept stack (T08_KEEP set); project $PROJECT, secrets in $TMP"; return; fi
  say "--- cleanup"
  C down -v --remove-orphans >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT

ST2() { docker exec -e ST2_API_URL=http://st2api:9101 -e ST2_AUTH_URL=http://st2auth:9100 ${ST2_AUTH_TOKEN:+-e ST2_AUTH_TOKEN=$ST2_AUTH_TOKEN} "$(cid st2actionrunner)" /opt/stackstorm/st2/bin/st2 "$@"; }
SQL() { docker exec -i "$(cid localpg)" psql -U postgres -v ON_ERROR_STOP=1 -qAt "$@"; }
RUNNER() { docker exec -i "$(cid st2actionrunner)" "$@"; }
upwait() { C up -d --wait --wait-timeout "$WAIT" "$@" >"$TMP/up.log" 2>&1 || { redact <"$TMP/up.log" | tail -12 | sed 's/^/    up: /'; return 1; }; }
# Adapter faults and sim state live on the st2 volume; written as root, readable/writable by st2.
set_faults() { printf '%s' "$1" | docker exec -i -u root "$(cid st2actionrunner)" sh -c "cat > $SIM/faults.json && chown st2:st2 $SIM/faults.json"; }
reset_sim() { docker exec -u root "$(cid st2actionrunner)" sh -c "rm -f $SIM/state.json $SIM/calls.jsonl $SIM/lock $SIM/faults.json; chown st2:st2 $SIM"; }
sim_calls() { RUNNER cat "$SIM/calls.jsonl" 2>/dev/null; }
count_calls() { sim_calls | python3 -c "import sys,json; print(sum(1 for l in sys.stdin if l.strip() and json.loads(l)['method']=='$1'))"; }
new_op() { ST2 run cdr.open_operation kind=scale_out source=automation fingerprint="$1" event_name=local_sim -j 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['result']['operation_id'])"; }
run_scale_out() {  # $1 operation id; prints st2 execution id, leaves JSON in $TMP/exec.json
  ST2 run cdr.scale_out operation_id="$1" -j >"$TMP/exec.json" 2>"$TMP/exec.err"
  # st2 run -j prints a progress snapshot and then the final execution: take the last JSON document.
  python3 -c "
import json
t=open('$TMP/exec.json').read(); dec=json.JSONDecoder(); i=0; d=None
while i < len(t):
    while i < len(t) and t[i].isspace(): i += 1
    if i >= len(t): break
    try:
        nd, i = dec.raw_decode(t, i)
    except ValueError:
        break
    if isinstance(nd, dict) and 'status' in nd: d = nd
print(d['id'], d['status'])
open('$TMP/exec.statuses','w').write(' '.join(l['status'] for l in d.get('log', [])))" 2>/dev/null
}
diag() {  # $1 execution id: show failing tasks without secrets
  ST2 execution get "$1" -j 2>/dev/null | redact | python3 -c "
import sys,json
d=json.load(sys.stdin); print('    execution', d['status'])
for e in (d.get('result') or {}).get('errors', [])[:5]: print('    error:', json.dumps(e)[:400])" 2>/dev/null
}
events_valid() {  # $1 operation id
  SQL -c "SELECT to_jsonb(r) FROM (SELECT e.event_id, e.sequence, e.event_type, e.status, e.step, e.message, e.source, e.occurred_at, e.operation_id, o.policy_version FROM ops.operation_events e JOIN ops.operations o ON o.id = e.operation_id WHERE e.operation_id = '$1' ORDER BY e.sequence) r" \
    | (cd "$MGMT" && pnpm -s exec tsx tests/compose/assert-events.ts)
}

# Evidence for any FAIL (first failure and the end of the run): every cdr.* execution with result/stdout/stderr, lease,
# slots, operations/attempts/incidents/events, sim state and call log, runner and workflow-engine logs.
# Lands in tests/compose/evidence/<UTC timestamp>/NN-<label>/ (gitignored). Best effort: never fails the run itself.
capture_evidence() {
  CAPTURING=1
  [ -n "$EVID" ] || EVID="$HERE/evidence/$(date -u +%Y%m%dT%H%M%SZ)"
  EVN=$((EVN + 1)); local d; d="$EVID/$(printf '%02d' "$EVN")-$1"; mkdir -p "$d"
  say "    evidence -> ${d#$MGMT/}"
  local q
  for q in "control_leases|SELECT application_key, holder_operation, lease_expires_at, fencing_generation, now() AS db_now FROM ops.control_leases" \
           "capacity_slots|SELECT key, provider, role, state, generation, lease_operation_id, bound_instance_id FROM ops.capacity_slots ORDER BY key" \
           "instances|SELECT slot_key, provider, provider_resource_id, lifecycle_state, slot_state, simulated FROM ops.instances ORDER BY created_at" \
           "operations|SELECT id, kind, status, current_step, outcome, adapter_mode, stackstorm_execution_id, created_at, updated_at, finished_at FROM ops.operations ORDER BY created_at" \
           "operation_attempts|SELECT operation_id, attempt, provider, slot_key, status, error_code, started_at, deadline_at FROM ops.operation_attempts ORDER BY operation_id, attempt" \
           "incidents|SELECT fingerprint, state, severity, description FROM ops.incidents ORDER BY opened_at" \
           "operation_events|SELECT sequence, operation_id, event_type, status, step, message, occurred_at FROM ops.operation_events ORDER BY occurred_at, sequence"; do
    SQL -c "COPY ($(printf '%s' "${q#*|}")) TO STDOUT WITH (FORMAT csv, HEADER true)" >"$d/${q%%|*}.csv" 2>&1
  done
  docker exec -u root "$(cid st2actionrunner)" sh -c "cat $SIM/state.json 2>/dev/null; echo; cat $SIM/calls.jsonl 2>/dev/null; echo; cat $SIM/faults.json 2>/dev/null" >"$d/sim-state-calls-faults.txt" 2>&1
  if [ -n "${ST2_AUTH_TOKEN:-}" ]; then
    docker exec -e ST2_AUTH_TOKEN="$ST2_AUTH_TOKEN" "$(cid st2actionrunner)" /opt/stackstorm/st2/bin/python -c "
import json, os, requests
h = {'X-Auth-Token': os.environ['ST2_AUTH_TOKEN']}
print(json.dumps(requests.get('http://st2api:9101/v1/executions', params={'limit': 500}, headers=h, timeout=60).json()))" 2>"$d/executions.err" \
    | redact | python3 -c "
import json, sys, os
d = sys.argv[1]
try:
    rows = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
rows.sort(key=lambda r: r.get('start_timestamp', ''))
lines = []
for r in rows:
    ref = r.get('action', {}).get('ref', '')
    if not ref.startswith('cdr.'):
        continue
    lines.append('%s %-34s %-10s parent=%s start=%s end=%s' % (r['id'], ref, r['status'], (r.get('parent') or '-'), r.get('start_timestamp', '-'), r.get('end_timestamp', '-')))
    with open(os.path.join(d, 'exec-%s-%s.json' % (r['id'], ref.replace('.', '_'))), 'w') as f:
        json.dump({k: r.get(k) for k in ('id', 'action', 'status', 'parameters', 'result', 'context', 'parent', 'start_timestamp', 'end_timestamp', 'log')}, f, indent=1)
open(os.path.join(d, 'executions.txt'), 'w').write('\n'.join(lines) + '\n')" "$d"
  fi
  docker logs --tail 400 "$(cid st2actionrunner)" 2>&1 | redact >"$d/actionrunner.log"
  docker logs --tail 400 "$(cid st2workflowengine)" 2>&1 | redact >"$d/workflowengine.log"
  docker logs --tail 200 "$(cid st2scheduler)" 2>&1 | redact >"$d/scheduler.log"
  docker inspect -f 'runner restarts={{.RestartCount}} oom={{.State.OOMKilled}}' "$(cid st2actionrunner)" >"$d/runner-state.txt" 2>&1
  CAPTURING=0
}

# No live capacity lease and no non-terminal operation may exist before a test starts; fail loudly, not flakily.
assert_idle() {
  local i live t0=$SECONDS
  for i in $(seq 1 30); do
    live=$(SQL -c "SELECT (SELECT count(*) FROM ops.control_leases WHERE holder_operation IS NOT NULL AND lease_expires_at > now()) || ' ' || (SELECT count(*) FROM ops.operations WHERE status NOT IN ('succeeded','failed','cancelled'))")
    [ "$live" = "0 0" ] && break; sleep 2
  done
  step_log "assert_idle $1" "$t0" "$([ "$live" = "0 0" ] && echo ok || echo FAIL)"
  expect "no live lease and no non-terminal operation before $1" "$live" "0 0"
}


lib_bringup() {
EVID="$HERE/evidence/$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$EVID"; STEP_LOG="$EVID/durations.log"  # set here, not lazily: wait_exec logs from a subshell
say "${RUN_LABEL:-T08/T09} LOCAL SIMULATION RUN - Docker Desktop on a developer Mac ($(uname -m)), NOT the management VM, no cloud resources"
say "provider adapter: simulation (flock'd JSON on the st2 volume); every instance row must carry simulated=true"
say "StackStorm images run as linux/amd64 (emulated on arm64); timings are NOT representative of the 2 vCPU / 4 GB VM"
say "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

rnd() { openssl rand -hex "$1"; }
PG_PW=$(rnd 12); ST2_PW=$(rnd 12); MONGO_PW=$(rnd 12); RMQ_PW=$(rnd 12); GRAF_PW=$(rnd 12); OPS_PW=$(rnd 12)
umask 077
cat >"$TMP/mgmt.env" <<ENV
SUPABASE_OPS_URL=postgres://ops_t08:${PG_PW}@localpg:5432/postgres
OPS_OPERATOR_PASSWORD=${OPS_PW}
OPS_BASIC_AUTH_HASH='not-used-in-this-run'
MONGO_ROOT_USER=mongoroot
MONGO_ROOT_PASSWORD=${MONGO_PW}
RABBITMQ_USER=rmq
RABBITMQ_PASSWORD=${RMQ_PW}
RESTORE_TEST_PG_PASSWORD=${PG_PW}
LOCAL_PG_PASSWORD=${PG_PW}
ACME_EMAIL=
ST2_API_URL=http://st2api:9101
T31_HTTPS_PORT=18445
T31_PG_PORT=${PGPORT}
ENV
printf '[database]\nusername = mongoroot\npassword = %s\n\n[messaging]\nurl = amqp://rmq:%s@rabbitmq:5672/\n' "$MONGO_PW" "$RMQ_PW" >"$TMP/secrets/st2.user.conf"
# The pack config the workflows read: simulation adapter, faults file on the shared volume (absent = no faults).
printf 'supabase_ops_url: "postgres://ops_t08:%s@localpg:5432/postgres"\nprovider_adapter: simulation\nsimulation_faults: %s/faults.json\npolicy_compile: simulation\n' "$PG_PW" "$SIM" >"$TMP/secrets/cdr-pack-config.yaml"
docker run --rm httpd:2.4-alpine htpasswd -nbB st2admin "$ST2_PW" >"$TMP/secrets/st2-htpasswd"
printf '%s' "$GRAF_PW" >"$TMP/secrets/grafana_admin_password"
: >"$TMP/secrets/st2_api_key"
[ -n "${T08_KEEP:-}" ] && printf '%s' "$ST2_PW" >"$TMP/st2pw"   # only for debugging a kept stack
chmod 600 "$TMP/secrets/"* "$TMP/mgmt.env"; umask 022

say "--- 1. Postgres, migrations through ops/0013, test policy"
upwait localpg && pass "localpg healthy" || { fail "localpg did not start"; exit 1; }
SUPABASE_MIGRATION_URL="postgres://postgres:${PG_PW}@127.0.0.1:${PGPORT}/postgres" pnpm --dir "$APP" -s migrate 2>&1 | redact | tail -2 | sed 's/^/    /'
expect "ops/0012 and ops/0013 applied" "$(SQL -c "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='ops' AND p.proname IN ('open_operation','open_incident','set_instance_state','supersede_queued','set_routing','hold_orphan_slot','set_policy_phase')")" 7
SQL -c "CREATE ROLE ops_t08 LOGIN PASSWORD '${PG_PW}' IN ROLE dr_ops, dr_automation" >/dev/null && pass "st2 login role is a member of dr_ops and dr_automation" || fail "login role"
(cd "$MGMT" && SUPABASE_OPS_URL="postgres://ops_t08:${PG_PW}@127.0.0.1:${PGPORT}/postgres" pnpm -s seed:policy 2>&1 | redact | tail -1 | sed 's/^/    /')
# Scaled test policy: checkpoints 30/45/60 s (deadline 60 s), lease heartbeat every 10 s. Everything else is the default policy.
SQL -c "UPDATE ops.policy_versions SET configuration = jsonb_set(jsonb_set(configuration, '{provisioning,readiness_checkpoints_seconds}', '[30,45,60]'), '{provisioning,readiness_deadline_seconds}', '60') WHERE status='active'" \
    -c "UPDATE ops.policy_versions SET configuration = jsonb_set(configuration, '{coordination,lease_renewal_seconds}', '10') WHERE status='active'" >/dev/null
say "    active policy: $(SQL -c "SELECT version, configuration #>> '{provisioning,readiness_checkpoints_seconds}', configuration #>> '{provisioning,readiness_deadline_seconds}', configuration #>> '{coordination,lease_renewal_seconds}' FROM ops.policy_versions WHERE status='active'")  (version, checkpoints, deadline, renewal)"

say "--- 2. StackStorm (emulated amd64: slow)"
# A cold MongoDB on the emulated stack is occasionally reported unhealthy on the first try (seen in 1 of 10 runs): retry once.
upwait mongo rabbitmq redis || { say "    stores not healthy on the first try; retrying once"; C restart mongo rabbitmq redis >/dev/null 2>&1; upwait mongo rabbitmq redis; } && pass "mongo, rabbitmq, redis healthy" || { fail "native stores"; exit 1; }
upwait st2api && pass "st2api healthy" || { fail "st2api"; exit 1; }
upwait st2api st2auth st2stream st2actionrunner st2scheduler st2workflowengine st2notifier st2garbagecollector st2rulesengine st2timersengine && pass "st2 services healthy" || { fail "st2 services"; C ps 2>&1 | sed 's/^/    /'; exit 1; }
C run --rm st2-register >"$TMP/register.log" 2>&1 && pass "st2-register --register-all (actions, workflows, policies, pack config)" || { fail "st2-register"; redact <"$TMP/register.log" | tail -25 | sed 's/^/    /'; exit 1; }
ST2_AUTH_TOKEN=$(ST2 auth st2admin -p "$ST2_PW" -t 2>/dev/null | tail -1)
[ -n "$ST2_AUTH_TOKEN" ] && pass "st2 token obtained (not printed)" || { fail "st2 token"; exit 1; }
# The 60 s reconcile timer rule is registered by st2-register; runs that assert exact states disable it (and call cdr.reconcile
# themselves) so a tick cannot change state mid-assertion. Set LOCAL_TIMER_ON=1 to leave it enabled.
if [ -z "${LOCAL_TIMER_ON:-}" ]; then ST2 rule disable cdr.reconcile_every_60s >/dev/null 2>&1 && pass "reconcile timer rule disabled for deterministic assertions" || fail "could not disable the reconcile timer rule"; fi
if ST2 run packs.setup_virtualenv packs=cdr >"$TMP/venv.log" 2>&1; then say "    observed: packs.setup_virtualenv succeeded"; else say "    observed: packs.setup_virtualenv did not complete (see t16-t30.local.out); finishing the venv from requirements.txt"; fi
upwait st2actionrunner && pass "st2actionrunner healthy" || fail "st2actionrunner after virtualenv step"
# pip needs the network from inside the runner; retry (the pack venv is a test prerequisite, not the thing under test) and show why on failure.
venv_ok=0
for try in 1 2 3; do
  if docker exec "$(cid st2actionrunner)" bash -c 'test -x /opt/stackstorm/virtualenvs/cdr/bin/pip || /opt/stackstorm/st2/bin/python -m virtualenv --always-copy /opt/stackstorm/virtualenvs/cdr >/dev/null; /opt/stackstorm/virtualenvs/cdr/bin/pip install -q -r /opt/stackstorm/packs/cdr/requirements.txt >/tmp/pip.log 2>&1; /opt/stackstorm/virtualenvs/cdr/bin/python -c "import psycopg"' 2>/dev/null; then venv_ok=1; break; fi
  say "    pack virtualenv attempt $try failed: $(docker exec "$(cid st2actionrunner)" tail -n 2 /tmp/pip.log 2>/dev/null | tr '\n' ' ' | cut -c1-200)"; sleep 5
done
[ "$venv_ok" = 1 ] && pass "pack virtualenv has psycopg" || fail "pack virtualenv psycopg"
docker exec -u root "$(cid st2actionrunner)" sh -c "mkdir -p $SIM && chown st2:st2 $SIM && chmod 700 $SIM" && pass "simulation state dir on the st2 volume" || fail "sim dir"

say "--- 3. registration: workflows, policies, actions/lib import on the live st2"
for ref in cdr.scale_out cdr.provision_and_deploy cdr.provision_attempt cdr.run_heavy cdr.reconcile cdr.reconcile_pass cdr.primary_failure cdr.scale_in cdr.activate_standby cdr.alert_intent cdr.open_operation cdr.provider_create; do
  ST2 action get "$ref" >/dev/null 2>&1 && pass "action $ref registered" || fail "action $ref missing"
done
ST2 policy list -p cdr -j >"$TMP/policies.json" 2>"$TMP/policies.err"
pol=$(python3 -c "import json; print(','.join(sorted(p['ref'] for p in json.load(open('$TMP/policies.json')))))" 2>/dev/null)
[ "$pol" = "cdr.capacity_guard.activate_standby,cdr.capacity_guard.apply_policy,cdr.capacity_guard.primary_failure,cdr.capacity_guard.scale_in,cdr.capacity_guard.scale_out,cdr.capacity_guard.standby_repair,cdr.heavy,cdr.reconcile_single" ] || { say "    policy list output: $(head -c 300 "$TMP/policies.json") $(head -c 200 "$TMP/policies.err")"; }
expect "policies registered" "$pol" "cdr.capacity_guard.activate_standby,cdr.capacity_guard.apply_policy,cdr.capacity_guard.primary_failure,cdr.capacity_guard.scale_in,cdr.capacity_guard.scale_out,cdr.capacity_guard.standby_repair,cdr.heavy,cdr.reconcile_single"
# adapter_info imports cdr_common -> cdr_db, lease_keeper, providers (the actions/lib modules): proves st2 puts actions/lib on the path.
ST2 run cdr.adapter_info -j >"$TMP/ai.json" 2>&1; ai=$(python3 -c "import json; d=json.load(open('$TMP/ai.json')); print(d['status'], d['result']['result']['adapter_mode'], d['result']['result']['simulated'])" 2>/dev/null)
expect "actions/lib imports on live st2 (cdr.adapter_info -> status adapter simulated)" "$ai" "succeeded simulation True"
reset_sim
RESTARTS0=$(docker inspect -f '{{.RestartCount}}' "$(cid st2actionrunner)")

}
