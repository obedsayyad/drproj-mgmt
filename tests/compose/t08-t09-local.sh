#!/bin/bash
# P4.2 live check of the provisioning path on the LOCAL SIMULATION adapter (T08, T09), run LOCALLY under Docker Desktop.
# This is NOT the management VM and nothing here is a real cloud: Postgres is a throwaway container, StackStorm 3.9.0
# images are amd64-only and run emulated on Apple silicon, and every VM is a row in the simulation adapter's JSON state
# (instances are written with simulated=true). Output is recorded in tests/compose/t08-t09.local.out.
#
#   tests/compose/t08-t09-local.sh
# Needs: docker, pnpm, node, python3, openssl. Set COMPOSE_BIN if `docker compose` is unavailable.
HERE=$(cd "$(dirname "$0")" && pwd)
MGMT=$(cd "$HERE/../.." && pwd)
APP=$(cd "$MGMT/../drproj-app" && pwd)
OUT=${T08_OUT:-$HERE/t08-t09.local.out}
PGPORT=${T08_PG_PORT:-55436}
PROJECT=drproj-t08
RUN_LABEL="T08/T09"
. "$HERE/lib-st2-local.sh"
lib_bringup

if [ -z "${T08_SKIP_T08:-}" ]; then   # T08_SKIP_T08=1 runs T09 only (debugging)
say "--- 4. T08: provider never ready on both providers (policy checkpoints 30/45/60 s)"
set_faults '{"digitalocean":{"never_ready":true},"linode":{"never_ready":true}}'
assert_idle T08
OP8=$(new_op t08)
[ -n "$OP8" ] && pass "operation opened by cdr.open_operation ($OP8)" || { fail "open_operation"; exit 1; }
expect "op captured the active policy version" "$(SQL -c "SELECT policy_version FROM ops.operations WHERE id='$OP8'")" "$(SQL -c "SELECT version FROM ops.policy_versions WHERE status='active'")"
read -r EX8 EXS8 < <(run_scale_out "$OP8")
expect "scale_out execution finished" "$EXS8" succeeded
case " $(cat "$TMP/exec.statuses" 2>/dev/null) " in *delayed*) fail "capacity_guard delayed the T08 execution ($(cat "$TMP/exec.statuses"))";; *) pass "capacity_guard did not delay T08";; esac
[ "$EXS8" = succeeded ] || diag "$EX8"
expect "operation status failed (exhausted)" "$(SQL -c "SELECT status || ' ' || coalesce(outcome->>'error','-') FROM ops.operations WHERE id='$OP8'")" "failed provision_exhausted"
expect "adapter_mode recorded on the operation" "$(SQL -c "SELECT adapter_mode FROM ops.operations WHERE id='$OP8'")" simulation
say "    attempts: $(SQL -c "SELECT string_agg(attempt || ':' || provider || ':' || status || ':' || coalesce(error_code,'-'), ' ' ORDER BY attempt) FROM ops.operation_attempts WHERE operation_id='$OP8'")"
expect "four attempts, all cleaned" "$(SQL -c "SELECT count(*) || ' ' || count(*) FILTER (WHERE status='cleaned') FROM ops.operation_attempts WHERE operation_id='$OP8'")" "4 4"
expect "attempt 2 retried the same provider as attempt 1" "$(SQL -c "SELECT (SELECT provider FROM ops.operation_attempts WHERE operation_id='$OP8' AND attempt=1) = (SELECT provider FROM ops.operation_attempts WHERE operation_id='$OP8' AND attempt=2)")" t
expect "attempts 3 and 4 used the other provider" "$(SQL -c "SELECT (SELECT provider FROM ops.operation_attempts WHERE operation_id='$OP8' AND attempt=3) <> (SELECT provider FROM ops.operation_attempts WHERE operation_id='$OP8' AND attempt=1) AND (SELECT provider FROM ops.operation_attempts WHERE operation_id='$OP8' AND attempt=4) = (SELECT provider FROM ops.operation_attempts WHERE operation_id='$OP8' AND attempt=3)")" t
expect "every attempt failed on the readiness deadline" "$(SQL -c "SELECT count(*) FROM ops.operation_attempts WHERE operation_id='$OP8' AND error_code='readiness_deadline'")" 4
expect "operation.waiting events: 3 checkpoints x 4 attempts" "$(SQL -c "SELECT count(*) FROM ops.operation_events WHERE operation_id='$OP8' AND event_type='operation.waiting'")" 12
expect "each of checkpoint 1/3, 2/3, 3/3 emitted once per attempt" "$(SQL -c "SELECT string_agg(n::text, ',' ORDER BY i) FROM (SELECT i, count(*) n FROM generate_series(1,3) i JOIN ops.operation_events e ON e.message LIKE 'readiness checkpoint ' || i || '/3%' AND e.operation_id='$OP8' GROUP BY i) x")" "4,4,4"
expect "infrastructure.provision.failed once per failed attempt" "$(SQL -c "SELECT count(*) FROM ops.operation_events WHERE operation_id='$OP8' AND event_type='infrastructure.provision.failed'")" 4
expect "exactly one infrastructure.provision.exhausted event" "$(SQL -c "SELECT count(*) FROM ops.operation_events WHERE operation_id='$OP8' AND event_type='infrastructure.provision.exhausted'")" 1
expect "exactly one open exhausted incident" "$(SQL -c "SELECT count(*) FROM ops.incidents WHERE fingerprint='provision.exhausted:$OP8' AND state='open'")" 1
expect "no other incident (no second exhausted loop)" "$(SQL -c "SELECT count(*) FROM ops.incidents")" 1
expect "all slots free at the end" "$(SQL -c "SELECT count(*) FROM ops.capacity_slots WHERE state <> 'free'")" 0
expect "every instance row is simulated=true and deleted" "$(SQL -c "SELECT count(*) || ' ' || bool_and(simulated) || ' ' || bool_and(lifecycle_state='deleted') FROM ops.instances")" "4 true true"
expect "lease released, one generation used" "$(SQL -c "SELECT coalesce(holder_operation::text,'free') || ' ' || fencing_generation FROM ops.control_leases")" "free 1"
expect "adapter created 4 resources" "$(count_calls create)" 4
order=$(sim_calls | python3 -c "
import sys,json
print(' '.join(r['method'][0] for r in map(json.loads, filter(str.strip, sys.stdin)) if r['method'] in ('create','delete')))")
expect "create is only followed by delete before the next create (cleanup confirmed first)" "$order" "c d c d c d c d"
expect "absence confirmed through get before each retry" "$(sim_calls | python3 -c "
import sys,json
rows=[json.loads(l) for l in sys.stdin if l.strip()]
ok=True; seen_delete=False; absent=False
for r in rows:
    if r['method']=='create' and seen_delete and not absent: ok=False
    if r['method']=='create': seen_delete=False; absent=False
    if r['method']=='delete': seen_delete=True
    if r['method']=='get' and r['result']=='absent': absent=True
print(ok)")" True
echo "    events schema: $(events_valid "$OP8")"; expect "T08 events validate against events.schema.json" "$(events_valid "$OP8" | cut -d' ' -f1)" ok

fi

if [ -z "${T08_SKIP_T09:-}" ]; then
say "--- 5. T09: create times out but the VM exists (ambiguous)"
assert_idle T09
[ -n "${T08_FORCE_FAIL:-}" ] && fail "forced failure (T08_FORCE_FAIL) to exercise evidence capture"
reset_sim; SQL -c "DELETE FROM ops.incident_events" -c "DELETE FROM ops.incidents" >/dev/null
set_faults '{"digitalocean":{"create_timeout":true},"linode":{"create_timeout":true}}'
OP9=$(new_op t09)
read -r EX9 EXS9 < <(run_scale_out "$OP9")
expect "scale_out execution finished" "$EXS9" succeeded
case " $(cat "$TMP/exec.statuses" 2>/dev/null) " in *delayed*) fail "capacity_guard delayed the T09 execution ($(cat "$TMP/exec.statuses"))";; *) pass "capacity_guard did not delay T09";; esac
[ "$EXS9" = succeeded ] || diag "$EX9"
expect "operation failed held_uncertain" "$(SQL -c "SELECT status || ' ' || coalesce(outcome->>'error','-') FROM ops.operations WHERE id='$OP9'")" "failed held_uncertain"
expect "one attempt, uncertain" "$(SQL -c "SELECT count(*) || ' ' || min(status) FROM ops.operation_attempts WHERE operation_id='$OP9'")" "1 uncertain"
SLOT9=$(SQL -c "SELECT key FROM ops.capacity_slots WHERE state='held_uncertain'")
PROV9=$(SQL -c "SELECT provider FROM ops.capacity_slots WHERE key='$SLOT9'")
expect "exactly one slot held_uncertain" "$(SQL -c "SELECT count(*) FROM ops.capacity_slots WHERE state<>'free'")" 1
say "    held slot: $SLOT9 on $PROV9"
OTHER9=$([ "$PROV9" = digitalocean ] && echo linode || echo digitalocean)
SQL -c "UPDATE ops.policy_versions SET configuration = jsonb_set(configuration, '{capacity,max_cloud_vms_per_provider}', jsonb_build_object('$PROV9', 1, '$OTHER9', 0)) WHERE status='active'" >/dev/null
expect "held_uncertain is counted against the cap (provider at 1, other at 0: nothing qualifies)" "$(SQL -c "SELECT coalesce(ops.next_elastic_provider('{}')::text, 'none')")" none
SQL -c "UPDATE ops.policy_versions SET configuration = jsonb_set(configuration, '{capacity,max_cloud_vms_per_provider}', '{\"digitalocean\":5,\"linode\":5}') WHERE status='active'" >/dev/null
expect "adapter create count stays 1" "$(count_calls create)" 1
expect "no delete and no cleanup attempted" "$(count_calls delete)" 0
expect "no fallback: no second attempt, no provision.exhausted" "$(SQL -c "SELECT (SELECT count(*) FROM ops.operation_attempts WHERE operation_id='$OP9') || ' ' || (SELECT count(*) FROM ops.operation_events WHERE operation_id='$OP9' AND event_type='infrastructure.provision.exhausted')")" "1 0"
expect "the ambiguously created resource still exists in the provider (not deleted by tag alone)" "$(RUNNER python3 -c "
import json; s=json.load(open('$SIM/state.json')); print(sum(1 for r in s['resources'].values() if not r['deleted']))")" 1
expect "one open provision.uncertain incident for the slot" "$(SQL -c "SELECT count(*) FROM ops.incidents WHERE fingerprint='provision.uncertain:$SLOT9' AND state='open'")" 1
expect "instance row simulated=true" "$(SQL -c "SELECT bool_and(simulated) FROM ops.instances WHERE slot_key='$SLOT9'")" t
expect "lease released after the stop" "$(SQL -c "SELECT coalesce(holder_operation::text,'free') FROM ops.control_leases")" free
expect "T09 events validate against events.schema.json" "$(events_valid "$OP9" | cut -d' ' -f1)" ok

fi

if [ -z "${T08_SKIP_BLIP:-}" ]; then
say "--- 5b. T09b: the database stops answering for ${BLIP_SECONDS:-25} s while scale_out holds the lease (fault injection)"
reset_sim; set_faults '{}'
assert_idle T09b
OPB=$(new_op t09b)
ST2 run cdr.scale_out operation_id="$OPB" -j >"$TMP/exec-b.json" 2>&1 &
BPID=$!
held=""
for i in $(seq 1 120); do
  held=$(SQL -c "SELECT coalesce(holder_operation::text,'') FROM ops.control_leases"); [ "$held" = "$OPB" ] && break; sleep 1
done
expect "scale_out took the lease before the blip" "$held" "$OPB"
docker pause "$(cid localpg)" >/dev/null && say "    localpg paused"
sleep "${BLIP_SECONDS:-25}"
docker unpause "$(cid localpg)" >/dev/null && say "    localpg resumed"
wait $BPID
free=""
for i in $(seq 1 60); do
  free=$(SQL -c "SELECT coalesce(holder_operation::text,'free') FROM ops.control_leases" 2>/dev/null); [ "$free" = free ] && break; sleep 2
done
expect "no stranded lease after the database came back" "$free" free
expect "the operation reached a terminal state" "$(SQL -c "SELECT CASE WHEN status IN ('succeeded','failed','cancelled') THEN 'terminal' ELSE status END FROM ops.operations WHERE id='$OPB'")" terminal
say "    operation: $(SQL -c "SELECT status, coalesce(outcome->>'error', outcome->>'noop_reason', '-') FROM ops.operations WHERE id='$OPB'")"
fi

say "--- 6. runner stayed up"
# (the packs.setup_virtualenv step above restarts the runner in this emulated setup, see t16-t30.local.out; counted before T08)
expect "st2actionrunner restarts during T08/T09" "$(docker inspect -f '{{.RestartCount}}' "$(cid st2actionrunner)")" "$RESTARTS0"

[ "$FAILN" -gt 0 ] && capture_evidence "final"
say "--- summary"
say "passed=$PASS failed=$FAILN"
[ "$FAILN" -eq 0 ]
