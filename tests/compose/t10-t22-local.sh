#!/bin/bash
# P4.3 live check of reconcile, primary_failure, scale_in and the alert->intent path on the LOCAL SIMULATION adapter
# (T10, T22, the restart gate, the orphan gate), run LOCALLY under Docker Desktop. NOT the management VM, no cloud: Postgres is a
# throwaway container, StackStorm 3.9.0 runs emulated amd64, every VM is a row in the simulation adapter's JSON state
# (instances carry simulated=true), and routing/drain are simulated too (HAProxy is P6). Output: tests/compose/t10-t22.local.out.
#
#   tests/compose/t10-t22-local.sh            # all
#   T10_ONLY="t22 orphan" tests/compose/t10-t22-local.sh   # a subset of: timer t10 fence t22 restart orphan
# Needs: docker, pnpm, node, python3, openssl. Shares bring-up and helpers with t08-t09-local.sh (lib-st2-local.sh).
HERE=$(cd "$(dirname "$0")" && pwd)
MGMT=$(cd "$HERE/../.." && pwd)
APP=$(cd "$MGMT/../drproj-app" && pwd)
OUT=${T08_OUT:-$HERE/t10-t22.local.out}
PGPORT=${T08_PG_PORT:-55437}
PROJECT=drproj-t10
RUN_LABEL="T10/T22"
export LOCAL_TIMER_ON=
. "$HERE/lib-st2-local.sh"
lib_bringup

only() { [ -z "${T10_ONLY:-}" ] || case " $T10_ONLY " in *" $1 "*) return 0;; *) return 1;; esac; }
. "$HERE/lib-st2-ops.sh"
POLICY=$(sq "SELECT version FROM ops.policy_versions WHERE status='active'")

# ---------------------------------------------------------------------------------------------------------------------------
if only timer; then
say "--- 4. the 60 s reconcile timer rule fires (IntervalTimer) and a startup-style manual run works"
reset_state
assert_idle timer
ST2 rule enable cdr.reconcile_every_60s >/dev/null 2>&1 && pass "timer rule enabled" || fail "enable timer rule"
wait_for "SELECT count(*) >= 2 FROM ops.operations WHERE kind='reconcile' AND status='succeeded'" 420 "two timer-driven reconcile operations"
ST2 rule disable cdr.reconcile_every_60s >/dev/null 2>&1
wait_idle 420
expect "timer-driven executions exist (rule trigger on the execution, no manual run)" "$(api_get "/executions?action=cdr.reconcile&limit=50" | python3 -c "
import json,sys
rows=json.load(sys.stdin); print(sum(1 for r in rows if (r.get('context') or {}).get('rule')))")" "$(api_get "/executions?action=cdr.reconcile&limit=50" | python3 -c "
import json,sys
rows=json.load(sys.stdin); print(len(rows))")"
expect "reconcile with nothing to do is a succeeded operation, no side effect" "$(sq "SELECT count(*) FROM ops.operations WHERE kind='reconcile' AND status='succeeded' AND coalesce(outcome->>'noop_reason','')=''")" "$(sq "SELECT count(*) FROM ops.operations WHERE kind='reconcile'")"
expect "no slot reserved, no incident" "$(sq "SELECT (SELECT count(*) FROM ops.capacity_slots WHERE state<>'free' AND key<>'warm') || ' ' || (SELECT count(*) FROM ops.incidents)")" "0 0"
expect "the seeded warm standby is explained (no orphan)" "$(sq "SELECT count(*) FROM ops.incidents WHERE fingerprint LIKE 'orphan:%'")" 0
fi

# ---------------------------------------------------------------------------------------------------------------------------
if only t10; then
say "--- 5. T10: primary_failure + scale_out + reconcile fired together (one capacity lease, fencing, one slot per operation)"
reset_state
assert_idle T10
OPPF=$(open_op primary_failure t10-pf); OPSO=$(open_op scale_out t10-so)
[ -n "$OPPF" ] && [ -n "$OPSO" ] && pass "operations opened ($OPPF, $OPSO)" || { fail "open_operation"; exit 1; }
EXPF_ID=$(st2_async cdr.primary_failure operation_id="$OPPF")
EXSO_ID=$(st2_async cdr.scale_out operation_id="$OPSO")
EXRC_ID=$(st2_async cdr.reconcile)
EXPF=$(wait_exec "$EXPF_ID" 900); EXSO=$(wait_exec "$EXSO_ID" 900); EXRC=$(wait_exec "$EXRC_ID" 600)
expect "primary_failure execution finished" "$EXPF" succeeded; expect "scale_out execution finished" "$EXSO" succeeded; expect "reconcile execution finished" "$EXRC" succeeded
wait_idle 420
say "    primary_failure: $(op_row "$OPPF")   scale_out: $(op_row "$OPSO")   reconcile: $(sq "SELECT string_agg(status || ' ' || coalesce(outcome->>'noop_reason','-'), ', ') FROM ops.operations WHERE kind='reconcile'")"
expect "primary_failure ended succeeded" "$(sq "SELECT status FROM ops.operations WHERE id='$OPPF'")" succeeded
expect "scale_out ended succeeded or a recorded no-op" "$(sq "SELECT status FROM ops.operations WHERE id='$OPSO'")" succeeded
expect "no operation failed" "$(sq "SELECT count(*) FROM ops.operations WHERE status='failed'")" 0
expect "each lease generation had exactly one holder (distinct generation per lease_acquired event)" "$(sq "SELECT count(*) FROM (SELECT message FROM ops.operation_events WHERE step='lease_acquired' GROUP BY message HAVING count(DISTINCT operation_id) > 1) x")" 0
expect "lease holders never overlapped in time (acquire .. finish intervals are disjoint)" "$(sq "WITH h AS (SELECT o.id, (SELECT min(e.occurred_at) FROM ops.operation_events e WHERE e.operation_id=o.id AND e.step='lease_acquired') t0, o.finished_at t1 FROM ops.operations o WHERE o.kind IN ('primary_failure','scale_out'))
 SELECT count(*) FROM h a JOIN h b ON a.id < b.id AND a.t0 IS NOT NULL AND b.t0 IS NOT NULL AND a.t0 < b.t1 AND b.t0 < a.t1")" 0
expect "each operation reserved at most one slot" "$(sq "SELECT coalesce(max(n),0) FROM (SELECT operation_id, count(DISTINCT slot_key) n FROM ops.operation_attempts GROUP BY operation_id) x")" 1
ELASTIC=$(sq "SELECT count(*) FROM ops.capacity_slots WHERE role='elastic' AND state='bound'")
say "    elastic slots bound: $ELASTIC"
expect "elastic VMs: one if scale_out ran first (reused), two if after; never more" "$(sq "SELECT $ELASTIC BETWEEN 1 AND 2")" t
expect "adapter create count equals the intended VMs (one per bound elastic slot)" "$(count_calls create)" "$ELASTIC"
expect "no (slot, op, attempt) was created twice" "$(sim_calls | python3 -c "
import sys,json
rows=[json.loads(l) for l in sys.stdin if l.strip() and json.loads(l)['method']=='create']
keys=[(r['slot'],r['op'],r['attempt']) for r in rows]; print(len(keys)-len(set(keys)))")" 0
expect "warm standby is now active and the primary is in simulated maintenance" "$(sq "SELECT routing_mode FROM ops.instances WHERE slot_key='warm'") $(RUNNER python3 -c "
import json; print(json.load(open('$SIM/state.json')).get('routing',{}).get('primary'))")" "active maint"
expect "no capacity-lease left behind" "$(sq "SELECT coalesce(holder_operation::text,'free') FROM ops.control_leases")" free
expect "primary.unhealthy incident open (the outage record)" "$(sq "SELECT count(*) FROM ops.incidents WHERE fingerprint='primary.unhealthy:relief' AND state='open'")" 1
expect "T10 primary_failure events validate against events.schema.json" "$(events_valid "$OPPF" | cut -d' ' -f1)" ok
expect "T10 scale_out events validate against events.schema.json" "$(events_valid "$OPSO" | cut -d' ' -f1)" ok
fi

# ---------------------------------------------------------------------------------------------------------------------------
if only fence; then
say "--- 6. T10b: an expired lease is taken over only by reconcile; the fenced worker gets CDR01 and creates nothing more"
reset_state
SQL -c "UPDATE ops.policy_versions SET configuration = jsonb_set(configuration, '{provisioning,readiness_deadline_seconds}', '240') WHERE status='active'" -c "UPDATE ops.policy_versions SET configuration = jsonb_set(configuration, '{provisioning,readiness_checkpoints_seconds}', '[60,120,240]') WHERE status='active'" >/dev/null
set_faults '{"linode":{"ready_after_s":120},"digitalocean":{"ready_after_s":120}}'
assert_idle T10b
OPF=$(open_op scale_out t10b)
st2_async cdr.scale_out operation_id="$OPF" >/dev/null
wait_for "SELECT EXISTS (SELECT 1 FROM ops.operation_attempts WHERE operation_id='$OPF' AND status='started') AND (SELECT count(*) FROM ops.operation_events WHERE operation_id='$OPF' AND step='create') > 0" 240 "scale_out waiting for readiness"
GEN0=$(sq "SELECT fencing_generation FROM ops.control_leases")
CREATES0=$(count_calls create)
sq "UPDATE ops.control_leases SET lease_expires_at = now() - interval '1 second'" >/dev/null   # the lease expires under the running worker (fault injection)
# a non-reconcile operation cannot take an expired lease: it is refused (slot_conflict) and keeps waiting
OPX=$(open_op primary_failure t10b-x)
st2_async cdr.primary_failure operation_id="$OPX" >/dev/null
sleep 25
expect "while expired and unreleased the lease is not taken by a non-reconcile operation" "$(sq "SELECT coalesce(holder_operation::text,'free') FROM ops.control_leases")" "$OPF"
RCS=$(reconcile_once | cut -d" " -f2)
expect "reconcile took the lease over (new fencing generation) and re-dispatched the interrupted operation under the same operation_id" "$(sq "SELECT (fencing_generation > $GEN0)::text FROM ops.control_leases") $(sq "SELECT count(*) FROM ops.operations r, jsonb_array_elements(r.outcome->'resumes') x WHERE r.kind='reconcile' AND x->'parameters'->>'operation_id' = '$OPF'")" "true 1"
expect "the interrupted operation was left reconciling (by the fenced worker or the takeover), never silently running" "$(sq "SELECT count(*) FROM ops.operation_events WHERE operation_id='$OPF' AND status='reconciling'")" 1
wait_idle 600
expect "the fenced worker's action ended lease_lost (CDR01), not as a success" "$(api_get "/executions?limit=500" | python3 -c "
import sys,json
rows=json.load(sys.stdin)
n=sum(1 for r in rows if r.get('action',{}).get('ref','').startswith('cdr.') and r['status'] == 'failed' and (r.get('parameters') or {}).get('operation_id') == '$OPF' and 'lease_lost' in json.dumps(r.get('result')))
print('yes' if n >= 1 else 'no')")" yes
expect "nothing was created after the lease was fenced (create count unchanged; the resume adopts the attempt by tags)" "$(sim_calls | python3 -c "
import sys,json
rows=[json.loads(l) for l in sys.stdin if l.strip()]
print(sum(1 for r in rows if r['method']=='create' and r['op']=='$OPF'))") $(count_calls create)" "1 $CREATES0"
expect "no operation left non-terminal and no lease stranded" "$(sq "SELECT (SELECT count(*) FROM ops.operations WHERE status NOT IN ('succeeded','failed','cancelled')) || ' ' || coalesce((SELECT holder_operation::text FROM ops.control_leases WHERE holder_operation IS NOT NULL),'free')")" "0 free"
SQL -c "UPDATE ops.policy_versions SET configuration = jsonb_set(jsonb_set(configuration, '{provisioning,readiness_checkpoints_seconds}', '[30,45,60]'), '{provisioning,readiness_deadline_seconds}', '60') WHERE status='active'" >/dev/null
fi

# ---------------------------------------------------------------------------------------------------------------------------
if only t22; then
say "--- 7. T22: scale-in (normal), then primary down + cpu.low -> refused (primary_outage); a queued scale-in is superseded"
reset_state
ELRID=$(seed_slot elastic-linode-1 linode elastic active)
assert_idle T22
say "    7a. normal scale-in through the cpu.low alert webhook (rule cdr_alert -> cdr.alert_intent -> cdr.scale_in)"
expect "webhook accepted" "$(post_alert app.cpu.low aa01)" 202
wait_for "SELECT EXISTS (SELECT 1 FROM ops.operations WHERE kind='scale_in' AND status IN ('succeeded','failed'))" 900 "scale_in operation finished"
OPSI=$(sq "SELECT id FROM ops.operations WHERE kind='scale_in' ORDER BY created_at LIMIT 1")
say "    scale_in: $(op_row "$OPSI")"
expect "scale_in succeeded with no noop_reason (a real removal)" "$(op_row "$OPSI")" "succeeded -"
expect "newest elastic slot released after confirmed deletion" "$(sq "SELECT state FROM ops.capacity_slots WHERE key='elastic-linode-1'")" free
expect "adapter deleted exactly that resource and it is absent" "$(count_calls delete) $(sim_present "$ELRID")" "1 0"
expect "the warm standby was not touched" "$(sq "SELECT state FROM ops.capacity_slots WHERE key='warm'") $(sim_present "$WARM_RID")" "bound 1"
expect "drain was simulated before the delete, routing removed first" "$(sim_calls | python3 -c "
import sys,json
m=[(r['method'],r.get('mode')) for r in map(json.loads, filter(str.strip, sys.stdin)) if r['method'] in ('set_routing','drain','delete')]
print(' '.join(a if b is None else a+':'+b for a,b in m))")" "set_routing:draining drain set_routing:none delete"
expect "slot.releasing then slot.released audited once" "$(sq "SELECT count(*) FILTER (WHERE action='slot.releasing') || ' ' || count(*) FILTER (WHERE action='slot.released') FROM ops.audit_events WHERE object='slot:elastic-linode-1'")" "1 1"
expect "scale_in events validate against events.schema.json" "$(events_valid "$OPSI" | cut -d' ' -f1)" ok
wait_idle 420

say "    7b. primary marked down (cpu.low during the outage is refused)"
ELRID=$(seed_slot elastic-linode-1 linode elastic active)
sq "DELETE FROM ops.audit_events" >/dev/null; DEL0=$(count_calls delete)
expect "primary_unhealthy webhook accepted" "$(post_alert app.primary.unhealthy bb01)" 202
wait_for "SELECT EXISTS (SELECT 1 FROM ops.operations WHERE kind='primary_failure' AND status IN ('succeeded','failed'))" 900 "primary_failure finished"
expect "primary_failure succeeded, reusing the existing elastic VM (exactly one elastic)" "$(sq "SELECT (SELECT status FROM ops.operations WHERE kind='primary_failure') || ' ' || (SELECT count(*) FROM ops.capacity_slots WHERE role='elastic' AND state<>'free')")" "succeeded 1"
expect "open primary.unhealthy incident exists" "$(sq "SELECT count(*) FROM ops.incidents WHERE fingerprint='primary.unhealthy:relief' AND state='open'")" 1
wait_idle 420
sq "DELETE FROM ops.audit_events" >/dev/null
expect "cpu.low webhook accepted during the outage" "$(post_alert app.cpu.low cc01)" 202
wait_for "SELECT EXISTS (SELECT 1 FROM ops.operations WHERE kind='scale_in' AND status IN ('succeeded','failed') AND outcome->>'noop_reason' IS NOT NULL)" 900 "refused scale_in finished"
OPSI2=$(sq "SELECT id FROM ops.operations WHERE kind='scale_in' AND outcome->>'noop_reason' IS NOT NULL ORDER BY created_at DESC LIMIT 1")
expect "scale_in succeeded as a no-op with noop_reason primary_outage" "$(op_row "$OPSI2")" "succeeded primary_outage"
expect "zero begin_slot_release (no slot.releasing audit)" "$(sq "SELECT count(*) FROM ops.audit_events WHERE action='slot.releasing'")" 0
expect "no provider delete and the elastic VM still exists, still bound" "$(( $(count_calls delete) - DEL0 )) $(sim_present "$ELRID") $(sq "SELECT state FROM ops.capacity_slots WHERE key='elastic-linode-1'")" "0 1 bound"
expect "the refusal was recorded with the step noop" "$(sq "SELECT count(*) FROM ops.operation_events WHERE operation_id='$OPSI2' AND step='noop'")" 1
wait_idle 420

say "    7c. a scale-in queued before the failure is superseded by primary_failure"
OPQ=$(open_op scale_in t22-queued)   # opened, never dispatched: queued
expect "scale_in is queued" "$(sq "SELECT status FROM ops.operations WHERE id='$OPQ'")" queued
expect "second primary_unhealthy webhook accepted" "$(post_alert app.primary.unhealthy dd01)" 202
wait_for "SELECT status='succeeded' FROM ops.operations WHERE id='$OPQ'" 420 "queued scale_in superseded"
expect "queued scale_in became a succeeded no-op superseded" "$(op_row "$OPQ")" "succeeded superseded"
expect "it never ran: no events but the supersede" "$(sq "SELECT count(*) FROM ops.operation_events WHERE operation_id='$OPQ' AND step IN ('start','lease_acquired')")" 0
wait_idle 420
fi

# ---------------------------------------------------------------------------------------------------------------------------
if only restart; then
say "--- 8. restart gate: the actionrunner is killed mid-create; reconcile adopts by operation_id (start_attempt created=false), no duplicate create"
reset_state
SQL -c "UPDATE ops.policy_versions SET configuration = jsonb_set(jsonb_set(configuration, '{provisioning,readiness_checkpoints_seconds}', '[200,400,600]'), '{provisioning,readiness_deadline_seconds}', '600') WHERE status='active'" >/dev/null
set_faults '{"digitalocean":{"post_create_latency_s":120},"linode":{"post_create_latency_s":120}}'
assert_idle restart
OPR=$(open_op scale_out t-restart)
st2_async cdr.scale_out operation_id="$OPR" >/dev/null
wait_for "SELECT count(*) > 0 FROM ops.operation_attempts WHERE operation_id='$OPR'" 420 "attempt recorded"
for i in $(seq 1 60); do [ "$(count_calls create)" = 1 ] && break; sleep 2; done
expect "the create call persisted one resource and is still in flight" "$(count_calls create)" 1
SLOTR=$(sq "SELECT slot_key FROM ops.operation_attempts WHERE operation_id='$OPR'")
expect "slot reserved but not bound at the kill" "$(sq "SELECT state FROM ops.capacity_slots WHERE key='$SLOTR'")" reserved
docker kill "$(cid st2actionrunner)" >/dev/null && say "    st2actionrunner killed (SIGKILL) mid-create"
set_faults '{}' 2>/dev/null || true
for i in $(seq 1 60); do docker exec "$(cid st2actionrunner)" true >/dev/null 2>&1 && break; sleep 3; done
upwait st2actionrunner && pass "st2actionrunner restarted and healthy" || fail "runner did not come back"
set_faults '{}'
expect "the killed worker's operation is not terminal and still holds a (soon expired) lease" "$(sq "SELECT status FROM ops.operations WHERE id='$OPR'")" running
say "    waiting for the lease to expire (<= 120 s) while calling reconcile every 20 s"
for i in $(seq 1 30); do
  reconcile_once >/dev/null
  [ "$(sq "SELECT status FROM ops.operations WHERE id='$OPR'")" != running ] && break
  sleep 20
done
wait_for "SELECT status IN ('succeeded','failed') FROM ops.operations WHERE id='$OPR'" 600 "resumed operation to finish"
say "    operation: $(op_row "$OPR")"
expect "the interrupted operation was resumed under the SAME operation_id and succeeded" "$(sq "SELECT status FROM ops.operations WHERE id='$OPR'")" succeeded
expect "reconcile recorded the takeover and the resume" "$(sq "SELECT count(*) FROM ops.operation_events WHERE operation_id='$OPR' AND step='reconcile_takeover'") $(sq "SELECT count(*) > 0 FROM ops.operation_events WHERE operation_id='$OPR' AND message LIKE '%adopted from execution%'")" "1 t"
expect "start_attempt answered created=false on the restart" "$(exec_results cdr.start_attempt | python3 -c "
import sys,json
rows=[json.loads(l) for l in sys.stdin if l.strip()]
print(sum(1 for r in rows if r['parameters'].get('operation_id') == '$OPR' and (r['result'] or {}).get('result', {}).get('created') is False))" | awk '{print ($1>=1) ? "yes" : "no"}')" yes
expect "NO duplicate create: the adapter created exactly one resource for the operation" "$(count_calls create)" 1
expect "one attempt, ready, on one slot" "$(sq "SELECT count(*) || ' ' || min(status) || ' ' || count(DISTINCT slot_key) FROM ops.operation_attempts WHERE operation_id='$OPR'")" "1 ready 1"
expect "slot bound to the one resource and the instance ready" "$(sq "SELECT s.state || ' ' || i.lifecycle_state FROM ops.capacity_slots s JOIN ops.instances i ON i.id=s.bound_instance_id WHERE s.key='$SLOTR'")" "bound ready"
expect "nothing was deleted" "$(count_calls delete)" 0
wait_idle 420  # the operation row turns succeeded a task before release_lease runs: wait for both, do not race the last task
expect "lease released, nothing non-terminal" "$(sq "SELECT coalesce(holder_operation::text,'free') FROM ops.control_leases") $(sq "SELECT count(*) FROM ops.operations WHERE status NOT IN ('succeeded','failed','cancelled')")" "free 0"
expect "events validate against events.schema.json" "$(events_valid "$OPR" | cut -d' ' -f1)" ok
SQL -c "UPDATE ops.policy_versions SET configuration = jsonb_set(jsonb_set(configuration, '{provisioning,readiness_checkpoints_seconds}', '[30,45,60]'), '{provisioning,readiness_deadline_seconds}', '60') WHERE status='active'" >/dev/null
fi

# ---------------------------------------------------------------------------------------------------------------------------
if only orphan; then
say "--- 9. orphan gate: a project-tagged resource nothing explains is held and reported, never deleted"
reset_state
assert_idle orphan
RID1=$(sim_inject digitalocean elastic-do-2 "$(UUID)" 1)   # names a free slot: slot held_uncertain + incident
RID2=$(sim_inject linode - "$(UUID)" 1)                      # no slot tag: incident only
expect "both resources exist before reconcile" "$(sim_present "$RID1")$(sim_present "$RID2")" 11
RCS=$(reconcile_once | cut -d" " -f2)
expect "reconcile execution succeeded" "$RCS" succeeded
expect "the slot named by the tags is held_uncertain (counted)" "$(sq "SELECT state FROM ops.capacity_slots WHERE key='elastic-do-2'")" held_uncertain
expect "instance row for the held slot is simulated and records the resource id" "$(sq "SELECT provider_resource_id = '$RID1' AND simulated FROM ops.instances WHERE slot_key='elastic-do-2'")" t
expect "one open critical incident per orphan (orphan:<provider>:<id>)" "$(sq "SELECT count(*) FROM ops.incidents WHERE state='open' AND severity='critical' AND fingerprint IN ('orphan:digitalocean:$RID1','orphan:linode:$RID2')")" 2
expect "the untagged-slot orphan did not invent a slot" "$(sq "SELECT count(*) FROM ops.capacity_slots WHERE state<>'free' AND key<>'warm'")" 1
expect "both resources STILL EXIST (never deleted by tag alone)" "$(sim_present "$RID1")$(sim_present "$RID2")" 11
expect "no delete call at all" "$(count_calls delete)" 0
expect "the held slot still counts against the provider cap" "$(sq "SELECT count(*) FROM ops.capacity_slots WHERE provider='digitalocean' AND state<>'free'")" 2
reconcile_once >/dev/null
expect "the next tick keeps it without incident spam: still 2 incidents, 1 event each" "$(sq "SELECT count(*) FROM ops.incidents WHERE fingerprint LIKE 'orphan:%'") $(sq "SELECT count(*) FROM ops.incident_events e JOIN ops.incidents i ON i.id=e.incident_id WHERE i.fingerprint LIKE 'orphan:%'")" "2 2"
expect "still nothing deleted after the second tick" "$(count_calls delete) $(sim_present "$RID1")$(sim_present "$RID2")" "0 11"
expect "reconcile operations succeeded and released the lease" "$(sq "SELECT count(*) FROM ops.operations WHERE kind='reconcile' AND status<>'succeeded'") $(sq "SELECT coalesce(holder_operation::text,'free') FROM ops.control_leases")" "0 free"
OPRC=$(sq "SELECT id FROM ops.operations WHERE kind='reconcile' ORDER BY created_at LIMIT 1")
expect "reconcile events validate against events.schema.json" "$(events_valid "$OPRC" | cut -d' ' -f1)" ok
fi

say "--- 10. runner stayed up (except the deliberate kill)"
say "    st2actionrunner restart count: $(docker inspect -f '{{.RestartCount}}' "$(cid st2actionrunner)") (informational; the restart gate kills the runner once, deliberately)"
[ "$FAILN" -gt 0 ] && capture_evidence "final"
say "--- summary"
say "passed=$PASS failed=$FAILN"
[ "$FAILN" -eq 0 ]
