#!/bin/bash
# Helpers shared by the live local-simulation checks that drive operations (t10-t22-local.sh, t27-t36-local.sh). Sourced after lib_bringup
# (lib-st2-local.sh): uses SQL, ST2, RUNNER, cid, SIM, TMP, say, pass, fail, step_log, reset_sim, set_faults from there. LOCAL SIMULATION only.
sq() { SQL -c "$1"; }
UUID() { python3 -c "import uuid; print(uuid.uuid4())"; }

api_get() { docker exec -e ST2_AUTH_TOKEN="$ST2_AUTH_TOKEN" "$(cid st2actionrunner)" /opt/stackstorm/st2/bin/python -c "
import os, sys, requests
print(requests.get('http://st2api:9101/v1' + sys.argv[1], headers={'X-Auth-Token': os.environ['ST2_AUTH_TOKEN']}, timeout=60).text)" "$1"; }
post_alert() {  # $1 event name  $2 fingerprint (hex); posts an Alertmanager-shaped notification to the cdr_alert webhook
  local body
  body=$(python3 -c "import json,sys; print(json.dumps({'alerts':[{'status':'firing','fingerprint':sys.argv[2],'labels':{'alertname':'Local','cdr_event':sys.argv[1],'policy_version':sys.argv[3]}}]}))" "$1" "$2" "$POLICY")
  printf '%s' "$body" | docker exec -i -e ST2_AUTH_TOKEN="$ST2_AUTH_TOKEN" "$(cid st2actionrunner)" /opt/stackstorm/st2/bin/python -c "
import os, sys, requests
r = requests.post('http://st2api:9101/v1/webhooks/cdr_alert', data=sys.stdin.read(), headers={'X-Auth-Token': os.environ['ST2_AUTH_TOKEN'], 'Content-Type': 'application/json'}, timeout=60)
print(r.status_code)"
}
sim_inject() {  # provider slot|- op attempt -> prints the new resource id (a project-tagged resource nobody asked for)
  RUNNER /opt/stackstorm/st2/bin/python -c "
import sys; sys.path.insert(0, '/opt/stackstorm/packs/cdr/actions/lib')
from providers.simulation import SimulationAdapter
tags = {'project': 'cdr'}
if sys.argv[2] != '-': tags['slot'] = sys.argv[2]
tags['op'] = sys.argv[3]; tags['attempt'] = sys.argv[4]
print(SimulationAdapter('$SIM').inject(sys.argv[1], tags))" "$@"
}
sim_present() {  # resource id -> 1 when the simulation still has it (not deleted)
  RUNNER python3 -c "
import json; s = json.load(open('$SIM/state.json')); r = s['resources'].get('$1'); print(1 if r and not r['deleted'] else 0)"
}
seed_slot() {  # key provider role routing -> seeds a bound, ready, simulated instance + its simulation resource (a labelled fixture, written as admin)
  local key=$1 prov=$2 role=$3 routing=$4 rid
  rid=$(sim_inject "$prov" "$key" "$(UUID)" 1)
  sq "WITH op AS (INSERT INTO ops.operations (kind, policy_version, status, adapter_mode) SELECT 'standby_repair', version, 'succeeded', 'simulation' FROM ops.policy_versions WHERE status='active' RETURNING id),
  i AS (INSERT INTO ops.instances (slot_key, provider, provider_resource_id, role, public_ip, vcpu, memory_mb, image_digest, lifecycle_state, routing_mode, routing_weight, slot_state, simulated, power_state)
        VALUES ('$key','$prov','$rid','$role','192.0.2.9',1,1024,'sha256:0000','ready','$routing',CASE WHEN '$routing'='active' THEN 100 ELSE 0 END,'bound',true,'on') RETURNING id)
  UPDATE ops.capacity_slots c SET state='bound', generation=c.generation+1, bound_instance_id=(SELECT id FROM i), lease_operation_id=(SELECT id FROM op), updated_at=now() WHERE c.key='$key'" >/dev/null
  echo "$rid"
}
clean_state() {  # clean ops state + simulation (no standby seeded)
  reset_sim; set_faults '{}'
  SQL -c "UPDATE ops.control_leases SET holder_operation = NULL, lease_expires_at = NULL" -c "DELETE FROM ops.audit_events" -c "DELETE FROM ops.operation_events" -c "DELETE FROM ops.incident_events" -c "DELETE FROM ops.incidents" \
     -c "DELETE FROM ops.operation_attempts" -c "UPDATE ops.capacity_slots SET state='free', bound_instance_id=NULL, lease_operation_id=NULL" -c "DELETE FROM ops.instances" \
     -c "DELETE FROM ops.processed_events" -c "DELETE FROM ops.operations" -c "UPDATE ops.runtime_flags SET maintenance=false, metrics_stale_since=NULL" >/dev/null
}
reset_state() {  # clean ops state + simulation; seed a ready warm standby (DigitalOcean, backup routing)
  clean_state
  WARM_RID=$(seed_slot warm digitalocean warm backup)
}
wait_for() {  # $1 sql returning t/f, $2 timeout seconds, $3 label
  local i n=$(($2 / 3)) t0=$SECONDS
  for i in $(seq 1 "$n"); do [ "$(sq "$1" 2>/dev/null)" = t ] && { step_log "wait_for: $3" "$t0" ok; return 0; }; sleep 3; done
  step_log "wait_for: $3 (budget ${2}s)" "$t0" TIMEOUT
  fail "timed out (${2}s) waiting for: $3"; return 1
}
all_terminal() { echo "SELECT NOT EXISTS (SELECT 1 FROM ops.operations WHERE status NOT IN ('succeeded','failed','cancelled'))"; }
wait_idle() { wait_for "$(all_terminal) AND NOT EXISTS (SELECT 1 FROM ops.control_leases WHERE holder_operation IS NOT NULL AND lease_expires_at > now())" "${1:-420}" "no non-terminal operation and no live lease"; }
open_op() { ST2 run cdr.open_operation kind="$1" source=automation fingerprint="$2" event_name=local_sim -j 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['result']['operation_id'])"; }
st2_async() {  # st2 run args... -> prints the new execution id (dispatch only: `st2 run` returns early on a delayed execution, so tests poll the database and the API)
  local out id
  out=$(ST2 run "$@" --async -j 2>"$TMP/async.err")
  id=$(printf '%s' "$out" | grep -oE '[0-9a-f]{24}' | head -1)
  [ -n "$id" ] || { say "    st2 run --async gave no execution id: $(printf '%s %s' "$out" "$(head -c 300 "$TMP/async.err")" | redact | head -c 400)"; return 1; }
  echo "$id"
}
exec_state() {  # execution id -> status
  [ -n "$1" ] || { echo "no-id"; return; }
  api_get "/executions/$1" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))"
}
wait_exec() {  # execution id, timeout seconds -> prints the terminal status (or the last one seen)
  local i st="" t0=$SECONDS
  for i in $(seq 1 $(($2 / 3))); do
    st=$(exec_state "$1"); case "$st" in succeeded|failed|canceled|abandoned|timeout) break;; esac; sleep 3
  done
  step_log "wait_exec $1 (budget ${2}s)" "$t0" "$st"
  echo "$st"
}
exec_status() {  # file -> "<id> <status>" of the last execution document in `st2 run -j` output
  python3 -c "
import json,sys
t=open(sys.argv[1]).read(); dec=json.JSONDecoder(); i=0; d=None
while i < len(t):
    while i < len(t) and t[i].isspace(): i += 1
    if i >= len(t): break
    try: nd, i = dec.raw_decode(t, i)
    except ValueError: break
    if isinstance(nd, dict) and 'status' in nd: d = nd
print(d['id'], d['status']) if d else print('- none')" "$1"
}
op_row() { sq "SELECT status || ' ' || coalesce(outcome->>'noop_reason', outcome->>'error', '-') FROM ops.operations WHERE id='$1'"; }
reconcile_once() {  # runs cdr.reconcile to completion (an overlapping tick is cancelled by policy: retry); prints "<id> <status>"
  local id st i
  for i in 1 2 3 4 5; do
    id=$(st2_async cdr.reconcile); st=$(wait_exec "$id" 420)
    [ "$st" != canceled ] && break; sleep 5
  done
  echo "$id $st"
}
last_reconcile_summary() { sq "SELECT outcome::text FROM ops.operations WHERE kind='reconcile' ORDER BY created_at DESC LIMIT 1"; }
exec_results() {  # action ref -> one JSON result per line (newest first)
  api_get "/executions?action=$1&limit=200" | python3 -c "
import json,sys
rows=json.load(sys.stdin)
rows.sort(key=lambda r: r.get('start_timestamp',''), reverse=True)
for r in rows: print(json.dumps({'status': r['status'], 'parameters': r.get('parameters'), 'result': r.get('result'), 'start': r.get('start_timestamp')}))"
}

