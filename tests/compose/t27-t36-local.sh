#!/bin/bash
# P4.4 live check of standby repair (T27), the interim warm standby (T36), apply_policy and the not_implemented skeletons on the LOCAL SIMULATION
# adapter, run LOCALLY under Docker Desktop. NOT the management VM, no cloud: Postgres is a throwaway container, StackStorm 3.9.0 runs emulated
# amd64, every VM is a row in the simulation adapter's JSON state (instances carry simulated=true), routing and the Prometheus reload are simulated
# too (HAProxy is P6). The standby.unhealthy producer is the simulation action cdr.standby_unhealthy. Output: tests/compose/t27-t36.local.out.
#
#   tests/compose/t27-t36-local.sh            # all
#   T27_ONLY="t27 policy" tests/compose/t27-t36-local.sh   # a subset of: t27 t36 policy skeleton
# Needs: docker, pnpm, node, python3, openssl. Shares bring-up and helpers with t08-t09 and t10-t22 (lib-st2-local.sh, lib-st2-ops.sh).
HERE=$(cd "$(dirname "$0")" && pwd)
MGMT=$(cd "$HERE/../.." && pwd)
APP=$(cd "$MGMT/../drproj-app" && pwd)
OUT=${T08_OUT:-$HERE/t27-t36.local.out}
PGPORT=${T08_PG_PORT:-55438}
PROJECT=drproj-t27
RUN_LABEL="T27/T36"
export LOCAL_TIMER_ON=
. "$HERE/lib-st2-local.sh"
lib_bringup

only() { [ -z "${T27_ONLY:-}" ] || case " $T27_ONLY " in *" $1 "*) return 0;; *) return 1;; esac; }
. "$HERE/lib-st2-ops.sh"

PINNED="sha256:0000000000000000000000000000000000000000000000000000000000000000"
for ref in cdr.standby_repair cdr.standby_unhealthy cdr.standby_classify cdr.standby_verify cdr.provider_power_on cdr.apply_policy cdr.policy_validate cdr.policy_compile \
           cdr.policy_verify cdr.policy_activate cdr.policy_rollback cdr.not_implemented cdr.failback cdr.start_load_test cdr.stop_load_test cdr.create_backup cdr.verify_backup cdr.restore_application; do
  ST2 action get "$ref" >/dev/null 2>&1 && pass "action $ref registered" || fail "action $ref missing"
done

produce() {  # sim_fault -> prints the standby_repair operation id opened and dispatched by the simulation producer
  ST2 run cdr.standby_unhealthy sim_fault="$1" -j 2>"$TMP/produce.err" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result']['result']['operation_id'])" 2>>"$TMP/produce.err"
}
wait_op() {  # operation id, seconds, label: waits for succeeded/failed
  wait_for "SELECT status IN ('succeeded','failed') FROM ops.operations WHERE id='$1'" "$2" "$3 finished"
}
op_outcome() { sq "SELECT status || ' ' || coalesce(outcome->>'noop_reason', outcome->>'error', outcome->>'path', '-') FROM ops.operations WHERE id='$1'"; }
lease_gen() { sq "SELECT fencing_generation FROM ops.control_leases"; }  # the fencing generation: it moves only when a lease is taken
sim_res() {  # resource id, field -> value in the simulation state
  RUNNER python3 -c "
import json,sys; r = json.load(open('$SIM/state.json'))['resources'].get(sys.argv[1]); print('none' if r is None else r.get(sys.argv[2]))" "$1" "$2"
}
sim_route() {  # slot key -> simulated routing mode
  RUNNER python3 -c "
import json,sys; print(json.load(open('$SIM/state.json')).get('routing', {}).get(sys.argv[1], 'unset'))" "$1"
}
sim_deploy() {  # provider resource-id -> marks the simulated app deployed (a labelled fixture: the standby was healthy before the fault)
  RUNNER /opt/stackstorm/st2/bin/python -c "
import sys; sys.path.insert(0, '/opt/stackstorm/packs/cdr/actions/lib')
from providers.simulation import SimulationAdapter
print(SimulationAdapter('$SIM', None).deploy(sys.argv[1], sys.argv[2])['result'])" "$@"
}
calls_of() {  # method [provider] -> number of adapter calls
  sim_calls | python3 -c "
import sys,json
rows=[json.loads(l) for l in sys.stdin if l.strip()]
print(sum(1 for r in rows if r['method']=='$1' and ('${2:-}'=='' or r.get('provider')=='${2:-}')))"
}
try_reserve() {  # provider kind -> number of slots reserved by a throwaway scale_out operation (0 = refused); the operation is closed again
  local op gen n
  op=$(sq "INSERT INTO ops.operations (kind, policy_version, status) SELECT 'scale_out', version, 'running' FROM ops.policy_versions WHERE status='active' RETURNING id" | head -1)
  gen=$(sq "SELECT generation FROM ops.acquire_lease('$op')")
  n=$(sq "SELECT count(*) FROM ops.reserve_slot('$op', $gen, '$1', '$2', true)")
  if [ "$n" != 0 ]; then
    # undo the probe: the slot goes back to free (a labelled fixture write as admin, not a product path)
    sq "UPDATE ops.capacity_slots SET state='free', bound_instance_id=NULL, lease_operation_id=NULL WHERE lease_operation_id='$op'; DELETE FROM ops.instances WHERE slot_state='reserved'" >/dev/null
  fi
  sq "SELECT ops.release_lease('$op', $gen)" >/dev/null; sq "UPDATE ops.operations SET status='succeeded', outcome='{\"noop_reason\":\"probe\"}' WHERE id='$op'" >/dev/null
  echo "$n"
}
warm_state() {  # slot key (default warm) -> "<lifecycle> <digest is pinned> <routing_mode>" of the instance currently bound to the slot
  sq "SELECT i.lifecycle_state || ' ' || (i.image_digest = '$PINNED') || ' ' || i.routing_mode FROM ops.capacity_slots c JOIN ops.instances i ON i.id=c.bound_instance_id WHERE c.key='${1:-warm}'"
}
warm_roles_bound() { sq "SELECT count(*) FROM ops.capacity_slots WHERE role='warm' AND state='bound'"; }
fast_ready() { local d='{"*":{"ready_after_s":1}}'; set_faults "${1:-$d}"; }

# ---------------------------------------------------------------------------------------------------------------------------
if only t27; then
say "--- 4. T27: warm standby repair, each classification driven by faults -> the expected path (local simulation, not real infrastructure)"
say "    4a. healthy (the record was stale): only state is written back, nothing is created, deployed or powered"
reset_state; fast_ready; sim_deploy digitalocean "$WARM_RID" >/dev/null; FIXDEP=$(calls_of deploy)
assert_idle T27h
OP=$(produce none); expect "producer opened a standby_repair operation (simulation)" "$(sq "SELECT kind || ' ' || count(*) FROM ops.operations WHERE id='$OP' GROUP BY kind")" "standby_repair 1"
wait_op "$OP" 600 "healthy repair"
expect "repair succeeded through the healthy path" "$(op_outcome "$OP")" "succeeded healthy"
expect "standby.unhealthy and standby.repaired events recorded" "$(sq "SELECT string_agg(event_type, ',' ORDER BY sequence) FROM ops.operation_events WHERE operation_id='$OP' AND event_type IN ('standby.unhealthy','standby.repaired')")" "standby.unhealthy,standby.repaired"
expect "no create, deploy (beyond the fixture's own), power-on or delete" "$(calls_of create) $(( $(calls_of deploy) - FIXDEP )) $(calls_of power_on) $(calls_of delete)" "0 0 0 0"
expect "ready, digest matches the pinned one, routing backup (database and simulated routing map)" "$(warm_state) $(sim_route warm)" "ready true backup backup"
wait_idle 420

say "    4b. (a) present but the app is broken -> redeploy into the same VM"
reset_state; fast_ready
assert_idle T27a
GENB=$(sq "SELECT generation FROM ops.capacity_slots WHERE key='warm'")
OP=$(produce app_broken); wait_op "$OP" 900 "redeploy repair"
expect "repair succeeded through the redeploy path" "$(op_outcome "$OP")" "succeeded redeploy"
expect "exactly one deploy of the SAME resource, no create, no power-on, no delete" "$(calls_of deploy) $(calls_of create) $(calls_of power_on) $(calls_of delete) $(sim_res "$WARM_RID" deleted)" "1 0 0 0 False"
expect "ready with the pinned digest and routing backup (HAProxy view simulated)" "$(warm_state) $(sim_route warm)" "ready true backup backup"
expect "the warm slot kept its generation (no release, no new reservation)" "$(sq "SELECT generation FROM ops.capacity_slots WHERE key='warm'")" "$GENB"
expect "the slot was never released" "$(sq "SELECT count(*) FROM ops.audit_events WHERE action IN ('slot.releasing','slot.released') AND object='slot:warm'")" 0
expect "events validate against events.schema.json" "$(events_valid "$OP" | cut -d' ' -f1)" ok
wait_idle 420
expect "lease released" "$(sq "SELECT coalesce(holder_operation::text,'free') FROM ops.control_leases")" free
wait_idle 420

say "    4c. (b) powered off -> power on, then redeploy"
reset_state; fast_ready
assert_idle T27b
OP=$(produce powered_off); wait_op "$OP" 900 "power-on repair"
expect "repair succeeded through the power-on path" "$(op_outcome "$OP")" "succeeded power_on_redeploy"
expect "one power-on, then one deploy; no create, no delete; the VM is on again" "$(calls_of power_on) $(calls_of deploy) $(calls_of create) $(calls_of delete) $(sim_res "$WARM_RID" power)" "1 1 0 0 on"
expect "power-on happened before the deploy" "$(sim_calls | python3 -c "
import sys,json
m=[json.loads(l)['method'] for l in sys.stdin if l.strip()]
print(m.index('power_on') < m.index('deploy'))")" True
expect "ready, pinned digest, routing backup" "$(warm_state) $(sim_route warm)" "ready true backup backup"
wait_idle 420

say "    4d. (c) confirmed absent -> release with absence evidence, reserve the warm slot again, provision into it"
reset_state; fast_ready
assert_idle T27c
GEN0=$(sq "SELECT generation FROM ops.capacity_slots WHERE key='warm'")
OP=$(produce absent); wait_op "$OP" 900 "reprovision repair"
expect "repair succeeded through the reprovision path" "$(op_outcome "$OP")" "succeeded reprovision"
expect "slot released once with provider-API absence evidence, then reserved and bound again" "$(sq "SELECT count(*) FILTER (WHERE action='slot.releasing') || ' ' || count(*) FILTER (WHERE action='slot.released' AND new_value ->> 'confirmed_absent' = 'true' AND new_value ->> 'source' = 'provider_api') || ' ' || count(*) FILTER (WHERE action='slot.reserved') FROM ops.audit_events WHERE object='slot:warm'")" "1 1 1"
expect "the warm slot has a new generation and is bound to a NEW resource" "$(sq "SELECT (c.generation > $GEN0) || ' ' || (i.provider_resource_id <> '$WARM_RID') || ' ' || c.state FROM ops.capacity_slots c JOIN ops.instances i ON i.id=c.bound_instance_id WHERE c.key='warm'")" "true true bound"
expect "exactly one create (the replacement), tagged for the warm slot of this operation; the old resource stays gone" "$(calls_of create) $(sim_res "$WARM_RID" deleted) $(sim_calls | python3 -c "
import sys,json
c=[json.loads(l) for l in sys.stdin if l.strip() and json.loads(l)['method']=='create'][0]
print(c['slot'], c['op']=='$OP', c['provider'])")" "1 True warm True digitalocean"
expect "ready, pinned digest, routing backup" "$(warm_state) $(sim_route warm)" "ready true backup backup"
expect "one warm-role slot bound" "$(warm_roles_bound)" 1
wait_idle 420

say "    4e. (d) provider answer unknown -> hold the slot, incident standby.uncertain, backoff to reconcile, NO create"
reset_state
set_faults '{"*":{"api_unknown":true}}'
assert_idle T27d
OP=$(produce none); wait_op "$OP" 600 "unknown repair"
expect "operation failed held_uncertain (nothing was repaired)" "$(op_outcome "$OP")" "failed held_uncertain"
expect "ZERO creates, deploys, power-ons and deletes on either provider" "$(calls_of create) $(calls_of deploy) $(calls_of power_on) $(calls_of delete)" "0 0 0 0"
expect "the warm slot is held_uncertain and still counted" "$(sq "SELECT state FROM ops.capacity_slots WHERE key='warm'") $(sq "SELECT count(*) FROM ops.capacity_slots WHERE provider='digitalocean' AND state<>'free'")" "held_uncertain 1"
expect "one open critical standby.uncertain:warm incident" "$(sq "SELECT count(*) FROM ops.incidents WHERE fingerprint='standby.uncertain:warm' AND state='open' AND severity='critical'")" 1
expect "no interim warm was reserved (the other provider does not answer either)" "$(sq "SELECT count(*) FROM ops.capacity_slots WHERE key LIKE 'interim-warm-%' AND state<>'free'")" 0
expect "the resource was not touched" "$(sim_res "$WARM_RID" deleted)" False
wait_idle 420
expect "lease released" "$(sq "SELECT coalesce(holder_operation::text,'free') FROM ops.control_leases")" free
wait_idle 420
fi

# ---------------------------------------------------------------------------------------------------------------------------
if only t36; then
say "--- 5. T36: warm provider api_unknown -> interim warm on the OTHER provider; both slots counted; after reconcile exactly one warm-role slot"
clean_state; fast_ready
assert_idle T36
say "    5a. the first standby is provisioned by the repair workflow itself (free warm slot -> reserve -> provision), so reconcile can later resolve it by its attempt tags"
OP=$(produce none); wait_op "$OP" 900 "initial standby provisioning"
expect "initial standby provisioned through the reprovision path" "$(op_outcome "$OP")" "succeeded reprovision"
WARM_RID=$(sq "SELECT provider_resource_id FROM ops.instances WHERE slot_key='warm'")
expect "warm slot bound on digitalocean (the warm provider), attempt recorded as ready" "$(sq "SELECT c.state || ' ' || c.provider FROM ops.capacity_slots c WHERE c.key='warm'") $(sq "SELECT count(*) FROM ops.operation_attempts WHERE slot_key='warm' AND status='ready'")" "bound digitalocean 1"
wait_idle 420
CREATES0=$(calls_of create); DOCREATES0=$(calls_of create digitalocean)

say "    5b. digitalocean stops answering: the repair holds the warm slot and reserves interim-warm-linode"
set_faults '{"digitalocean":{"api_unknown":true},"*":{"ready_after_s":1}}'
OP=$(produce none); wait_op "$OP" 900 "interim warm repair"
expect "repair succeeded through the interim path" "$(op_outcome "$OP")" "succeeded interim"
expect "warm slot held_uncertain, interim-warm-linode bound" "$(sq "SELECT string_agg(key || '=' || state, ' ' ORDER BY key) FROM ops.capacity_slots WHERE role='warm' AND state<>'free'")" "interim-warm-linode=bound warm=held_uncertain"
expect "the cap count includes the held warm slot on digitalocean and the interim slot on linode" "$(sq "SELECT string_agg(provider || '=' || n, ' ' ORDER BY provider) FROM (SELECT provider, count(*) n FROM ops.capacity_slots WHERE state<>'free' GROUP BY provider) x")" "digitalocean=1 linode=1"
expect "exactly one create happened, on linode; none on the warm provider" "$(( $(calls_of create) - CREATES0 )) $(( $(calls_of create digitalocean) - DOCREATES0 ))" "1 0"
expect "the interim VM carries the interim slot tag" "$(sim_calls | python3 -c "
import sys,json
c=[json.loads(l) for l in sys.stdin if l.strip() and json.loads(l)['method']=='create'][-1]
print(c['slot'], c['provider'])")" "interim-warm-linode linode"
expect "the interim standby is ready with the pinned digest and routing backup" "$(warm_state interim-warm-linode) $(sim_route interim-warm-linode)" "ready true backup backup"
expect "one open standby.uncertain:warm incident" "$(sq "SELECT count(*) FROM ops.incidents WHERE fingerprint='standby.uncertain:warm' AND state='open'")" 1
wait_idle 420

say "    5c. a reservation beyond the cap is refused (linode cap lowered to 1 for the probe; the interim slot occupies it)"
sq "UPDATE ops.policy_versions SET configuration = jsonb_set(configuration, '{capacity,max_cloud_vms_per_provider,linode}', '1') WHERE status='active'" >/dev/null
expect "an elastic reservation on linode is refused" "$(try_reserve linode elastic)" 0
sq "UPDATE ops.policy_versions SET configuration = jsonb_set(configuration, '{capacity,max_cloud_vms_per_provider,linode}', '5') WHERE status='active'" >/dev/null
expect "the held warm slot also still counts against digitalocean" "$(sq "SELECT count(*) FROM ops.capacity_slots WHERE provider='digitalocean' AND state<>'free'")" 1

say "    5d. digitalocean answers again: reconcile resolves the warm slot (attempt tags), requests a repair, the repair restores it; reconcile then drains the surplus interim"
set_faults '{"*":{"ready_after_s":1}}'
RCS=$(reconcile_once | cut -d" " -f2); expect "reconcile succeeded" "$RCS" succeeded
expect "reconcile resolved the held warm slot with provider-API evidence" "$(sq "SELECT count(*) FROM ops.audit_events WHERE action='slot.resolved_bound' AND object='slot:warm' AND new_value ->> 'source' = 'provider_api'")" 1
expect "reconcile opened exactly one standby repair for it" "$(sq "SELECT count(*) FROM ops.processed_events WHERE fingerprint LIKE 'reconcile.warm_mismatch:warm:%' AND source='reconciliation'")" 1
OPR=$(sq "SELECT operation_id FROM ops.processed_events WHERE fingerprint LIKE 'reconcile.warm_mismatch:warm:%'")
wait_op "$OPR" 900 "reconcile-requested repair"
expect "the requested repair succeeded (healthy path: the VM was fine, the record was not)" "$(op_outcome "$OPR")" "succeeded healthy"
expect "both warm-role slots are bound for a moment (surplus)" "$(warm_roles_bound)" 2
wait_idle 420
DEL0=$(calls_of delete)
RCS=$(reconcile_once | cut -d" " -f2); expect "second reconcile succeeded" "$RCS" succeeded
expect "reconcile opened the surplus scale-in" "$(sq "SELECT count(*) FROM ops.processed_events WHERE fingerprint='reconcile.surplus_warm:interim-warm-linode'")" 1
OPS=$(sq "SELECT operation_id FROM ops.processed_events WHERE fingerprint='reconcile.surplus_warm:interim-warm-linode'")
wait_op "$OPS" 900 "surplus scale-in"
expect "scale_in succeeded with a real removal (no noop reason)" "$(op_outcome "$OPS")" "succeeded -"
expect "exactly ONE warm-role slot bound after resolution: the original warm slot" "$(warm_roles_bound) $(sq "SELECT key FROM ops.capacity_slots WHERE role='warm' AND state='bound'")" "1 warm"
expect "the surplus slot was released with provider-API absence evidence" "$(sq "SELECT state FROM ops.capacity_slots WHERE key='interim-warm-linode'") $(sq "SELECT count(*) FROM ops.audit_events WHERE action='slot.released' AND object='slot:interim-warm-linode' AND new_value ->> 'confirmed_absent' = 'true' AND new_value ->> 'source' = 'provider_api'")" "free 1"
expect "exactly one delete, of the interim VM; the warm VM is untouched" "$(( $(calls_of delete) - DEL0 )) $(sim_present "$WARM_RID") $(sim_res "$(sq "SELECT provider_resource_id FROM ops.instances WHERE slot_key='interim-warm-linode' ORDER BY created_at DESC LIMIT 1")" deleted)" "1 1 True"
expect "slot counts back to one warm VM on digitalocean, none on linode" "$(sq "SELECT count(*) FROM ops.capacity_slots WHERE state<>'free'") $(sq "SELECT provider FROM ops.capacity_slots WHERE state<>'free'")" "1 digitalocean"
expect "the warm standby serves as backup again" "$(warm_state | awk '{print $1, $3}') $(sim_route warm)" "ready backup backup"
wait_idle 420
fi

# ---------------------------------------------------------------------------------------------------------------------------
policy_fixture() {  # version jsonb-set-path new-json-value -> inserts a PENDING version (active config with one change) and an apply_policy operation; prints the operation id
  # A labelled fixture written as admin: it is what PATCH /api/policies does in one transaction (the ops API container is not part of this stack).
  sq "INSERT INTO ops.policy_versions (version, configuration, status, activation_phase) SELECT $1, jsonb_set(configuration, '$2', '$3'), 'pending', 'validating' FROM ops.policy_versions WHERE status='active'" >/dev/null
  sq "INSERT INTO ops.operations (kind, policy_version, status, parameters) SELECT 'apply_policy', version, 'queued', '{\"policy_version\": $1}'::jsonb FROM ops.policy_versions WHERE status='active' RETURNING id" | head -1
}
prom_label() { RUNNER python3 -c "
import re,sys; t=open('$SIM/prometheus/$1').read(); print(re.search(r'policy_version:\s*\"([^\"]*)\"', t).group(1))"; }
if only policy; then
say "--- 6. apply_policy (P4 scope): validate -> Prometheus policy_version label re-rendered + reload (SIMULATED) -> verify -> activate; failure keeps the old version"
reset_state
docker exec -u root -i "$(cid st2actionrunner)" sh -c "mkdir -p $SIM/prometheus && cat > $SIM/prometheus/prometheus.yml && cp $SIM/prometheus/prometheus.yml $SIM/prometheus/prometheus.loaded.yml && chown -R st2:st2 $SIM/prometheus" <"$MGMT/deploy/compose/mgmt/prometheus/prometheus.yml" \
  && pass "simulated Prometheus seeded with a copy of deploy/compose/mgmt/prometheus/prometheus.yml" || fail "seed simulated prometheus"
expect "seeded label is the active version" "$(prom_label prometheus.yml)" "$(sq "SELECT version FROM ops.policy_versions WHERE status='active'")"
V1=$(sq "SELECT version FROM ops.policy_versions WHERE status='active'")
assert_idle policy

say "    6a. happy path (scale_out.cooldown_seconds 60 -> 90; no capacity change, so no lease)"
LG0=$(lease_gen); V2=$((V1 + 1)); OP=$(policy_fixture $V2 '{scale_out,cooldown_seconds}' '90')
ID=$(st2_async cdr.apply_policy operation_id="$OP"); wait_op "$OP" 600 "apply_policy"
expect "apply_policy succeeded" "$(op_outcome "$OP")" "succeeded -"
expect "new version active, old superseded, phase completed" "$(sq "SELECT string_agg(version || '=' || status || '/' || coalesce(activation_phase,'-'), ' ' ORDER BY version) FROM ops.policy_versions")" "$V1=superseded/completed $V2=active/completed"
expect "Prometheus label re-rendered AND loaded (file and the config the simulated reload made live)" "$(prom_label prometheus.yml) $(prom_label prometheus.loaded.yml)" "$V2 $V2"
expect "exactly one simulated reload" "$(calls_of prometheus_reload)" 1
expect "events: started, policy.apply.requested, succeeded" "$(sq "SELECT string_agg(event_type, ',' ORDER BY sequence) FROM ops.operation_events WHERE operation_id='$OP' AND event_type IN ('operation.started','policy.apply.requested','operation.succeeded')")" "operation.started,policy.apply.requested,operation.succeeded"
expect "no capacity lease was taken (the fencing generation did not move)" "$(lease_gen)" "$LG0"
expect "events validate against events.schema.json" "$(events_valid "$OP" | cut -d' ' -f1)" ok
wait_idle 420

say "    6b. failed activation: the reload fails -> old version stays active, new one failed, label restored"
V3=$((V2 + 1)); set_faults '{"*":{"prometheus_reload_fail":true}}'; OP=$(policy_fixture $V3 '{scale_out,cooldown_seconds}' '120')
ID=$(st2_async cdr.apply_policy operation_id="$OP"); wait_op "$OP" 600 "failing apply_policy"
expect "apply_policy failed with reload_failed" "$(op_outcome "$OP")" "failed reload_failed"
expect "the old version is still active; the new one is failed with the error recorded" "$(sq "SELECT string_agg(version || '=' || status, ' ' ORDER BY version) FROM ops.policy_versions") $(sq "SELECT apply_result ->> 'error' FROM ops.policy_versions WHERE version=$V3")" "$V1=superseded $V2=active $V3=failed reload_failed"
expect "the label in the file is back to the active version and nothing newer was loaded" "$(prom_label prometheus.yml) $(prom_label prometheus.loaded.yml)" "$V2 $V2"
wait_idle 420; set_faults '{}'

say "    6c. a cap lowered below the occupied count: pending transition, nothing deleted, no new reservation above the cap (exclusive lease)"
for k in 1 2 3; do seed_slot elastic-linode-$k linode elastic active >/dev/null; done
DEL0=$(calls_of delete); LG0=$(lease_gen)
V4=$((V3 + 1)); OP=$(policy_fixture $V4 '{capacity,max_cloud_vms_per_provider,linode}' '2')
ID=$(st2_async cdr.apply_policy operation_id="$OP"); wait_op "$OP" 600 "cap-lowering apply_policy"; wait_idle 420
LGD=$(( $(lease_gen) - LG0 ))   # read before try_reserve below, which takes a lease of its own
expect "apply_policy succeeded and reports the pending transition" "$(op_outcome "$OP") $(sq "SELECT outcome -> 'pending_transition' -> 0 ->> 'provider' || ':' || (outcome -> 'pending_transition' -> 0 ->> 'occupied') || '>' || (outcome -> 'pending_transition' -> 0 ->> 'cap') FROM ops.operations WHERE id='$OP'")" "succeeded - linode:3>2"
expect "version active" "$(sq "SELECT status FROM ops.policy_versions WHERE version=$V4")" active
expect "the three linode slots are still bound and nothing was deleted" "$(sq "SELECT count(*) FROM ops.capacity_slots WHERE provider='linode' AND state='bound'") $(( $(calls_of delete) - DEL0 ))" "3 0"
expect "a new reservation on linode is refused" "$(try_reserve linode elastic)" 0
expect "the exclusive lease was taken once (generation +1) and released" "$LGD $(sq "SELECT coalesce(holder_operation::text,'free') FROM ops.control_leases")" "1 free"
wait_idle 420

say "    6d. a change whose compile target is a later phase (HAProxy health) fails not_implemented and applies nothing"
RELOADS0=$(calls_of prometheus_reload)
V5=$((V4 + 1)); OP=$(policy_fixture $V5 '{health,interval_seconds}' '5')
ID=$(st2_async cdr.apply_policy operation_id="$OP"); wait_op "$OP" 600 "not_implemented apply_policy"
expect "apply_policy failed not_implemented" "$(op_outcome "$OP")" "failed not_implemented"
expect "the detail names the HAProxy setting" "$(sq "SELECT outcome -> 'detail' -> 'haproxy' ->> 0 FROM ops.operations WHERE id='$OP'")" health.interval_seconds
expect "active version unchanged, the new one failed, no reload happened" "$(sq "SELECT status FROM ops.policy_versions WHERE version=$V4") $(sq "SELECT status FROM ops.policy_versions WHERE version=$V5") $(( $(calls_of prometheus_reload) - RELOADS0 ))" "active failed 0"
wait_idle 420
fi

# ---------------------------------------------------------------------------------------------------------------------------
if only skeleton; then
say "--- 7. P6/P7 skeletons: an accidental trigger fails the operation with not_implemented and touches nothing"
reset_state
assert_idle skeleton
declare -a KINDS=(failback start_load_test stop_load_test create_backup verify_backup restore_application)
declare -a FIRST=(direct_probes validate_bounds stop_k6 backup_lease isolated_restore validate_verified_backup)
for i in "${!KINDS[@]}"; do
  k=${KINDS[$i]}; first=${FIRST[$i]}
  OP=$(sq "INSERT INTO ops.operations (kind, policy_version, status) SELECT '$k', version, 'queued' FROM ops.policy_versions WHERE status='active' RETURNING id" | head -1)
  SNAP0=$(sq "SELECT md5(string_agg(key || state || generation::text, ',' ORDER BY key)) FROM ops.capacity_slots") ; CALLS0=$(sim_calls | wc -l | tr -d ' ')
  ID=$(st2_async "cdr.$k" operation_id="$OP"); wait_op "$OP" 300 "$k skeleton"
  expect "$k: operation failed with code not_implemented" "$(op_outcome "$OP")" "failed not_implemented"
  expect "$k: the failing step is the first planned step ($first)" "$(sq "SELECT outcome ->> 'step' FROM ops.operations WHERE id='$OP'")" "$first"
  expect "$k: no lease, no slot change, no adapter call" "$(sq "SELECT coalesce(holder_operation::text,'free') FROM ops.control_leases") $(sq "SELECT md5(string_agg(key || state || generation::text, ',' ORDER BY key)) FROM ops.capacity_slots") $(sim_calls | wc -l | tr -d ' ')" "free $SNAP0 $CALLS0"
  expect "$k: no success event" "$(sq "SELECT count(*) FROM ops.operation_events WHERE operation_id='$OP' AND event_type='operation.succeeded'")" 0
  expect "$k: events validate against events.schema.json" "$(events_valid "$OP" | cut -d' ' -f1)" ok
done
wait_idle 420
fi

say "--- 8. runner stayed up"
say "    st2actionrunner restart count: $(docker inspect -f '{{.RestartCount}}' "$(cid st2actionrunner)") (expected equal to the start: $RESTARTS0)"
expect "no st2actionrunner restart during the run" "$(docker inspect -f '{{.RestartCount}}' "$(cid st2actionrunner)")" "$RESTARTS0"
[ "$FAILN" -gt 0 ] && capture_evidence "final"
say "--- summary"
say "passed=$PASS failed=$FAILN"
[ "$FAILN" -eq 0 ]
