# cdr StackStorm pack (P3.1 keep-alive, P3.2 alert recording, P4.1 capacity actions, P4.2 provisioning path, P4.3 reconcile and failover, P4.4 standby repair and apply_policy, P5.2 real adapter, P5.3 ansible_deploy)

- `cdr.keepalive` runs `SELECT 1` and appends a row to `ops.keepalive_runs` (migration `ops/0007`).
  The ops API exposes the newest success as `cdr_supabase_keepalive_last_success_timestamp`.
- `cdr.keepalive_every_6h` fires it on `core.st2.CronTimer` (00/06/12/18:01 UTC; an IntervalTimer restarts on every rule re-registration and never fired across frequent deploys).
- `cdr.cdr_alert` (rule) receives the Alertmanager webhook `POST /v1/webhooks/cdr_alert` (St2-Api-Key) and runs
  `cdr.alert_intent` (P4.3), which upserts firing alerts into `ops.processed_events` (`source=alertmanager`, migration
  `ops/0008`, the P3.2 `cdr.record_alert` behaviour) and maps the `cdr_event` label to a capacity intent (below).
- Config: `/opt/stackstorm/configs/cdr.yaml` (from `deploy/compose/mgmt/secrets/cdr-pack-config.yaml`, gitignored).

Registering on a running stack (not exercised in P3.1; needs st2 up):

    docker compose --profile init run --rm st2-register
    # build the pack virtualenv once, then reload
    st2 run packs.setup_virtualenv packs=cdr
    st2 action get cdr.keepalive

In the local emulated smoke (`tests/compose/t16-t30-local.sh`) `packs.setup_virtualenv` took the actionrunner container
down mid-pip, so the script completes the venv with
`/opt/stackstorm/virtualenvs/cdr/bin/pip install -r /opt/stackstorm/packs/cdr/requirements.txt` and imports `psycopg`
to verify it. Do the same if the action does not finish on the VM; the drproj-runner image (P4/P5) should bake it in.

## Capacity actions (P4.1, migration `ops/0009`)

Thin typed actions over the `ops.*` functions; the login in `supabase_ops_url` must be a member of `dr_ops` and `dr_automation`.
`acquire_lease`, `renew_lease`, `release_lease`, `assert_lease`, `reserve_slot` (`simulated` is a required parameter), `begin_slot_release`, `release_slot` (needs provider-API
absence evidence) and `update_ops_state`. They return short codes (`lease_lost`, `slot_conflict`, `invalid_transition`, `bad_argument`),
never driver text. Shared code is in `actions/lib/cdr_db.py`; that StackStorm puts `actions/lib` on the import path is assumed from the
StackStorm docs; P4.2 exercised it on a live st2 (`tests/compose/t08-t09-local.sh`, `cdr.adapter_info` imports `cdr_common`, `lease_keeper` and `providers`).

Tests: `pytest tests -q` from this directory with `TEST_DATABASE_URL` set (psycopg 3.3 needs Python 3.10, as the st2 image has).
Without st2common installed, `tests/conftest.py` registers a stand-in `Action` class; that is a test double, not StackStorm.
The keep-alive and alert tests also need `KEEPALIVE_TEST_URL` (a login that is a member of `dr_ops`); without it they skip.

## Provisioning path (P4.2, migration `ops/0011`) - local simulation only

`cdr.scale_out` -> `cdr.provision_and_deploy` -> `cdr.provision_attempt` (Orquesta, `actions/workflows/`). Status is written only through
`cdr.update_ops_state`; events are `operation.started/step.completed/waiting/succeeded/failed`, `infrastructure.provision.failed/exhausted`.

- scale_out: `adapter_info` -> `update_ops_state` (running, execution id, adapter mode) -> `acquire_lease` -> `capacity_gate`
  (maintenance, cooldown, fresh metrics; a refusal is a succeeded no-op with `noop_reason`) -> `next_elastic_provider` -> `reserve_slot`
  -> `provision_and_deploy` -> `release_lease`. Any task failure goes to `fail_operation` (`lease_lost` leaves the operation `reconciling` and
  does not touch the lease).
- provision_attempt: `start_attempt` (before the create) -> `provider_create` (tags `project=cdr`, slot, op, attempt) -> `bind_slot` ->
  `run_heavy` deploy -> `wait_ready` (policy checkpoints, one `operation.waiting` each, admits early) -> ready, or failed -> `provider_cleanup`
  (delete, confirm absent through the provider API) -> `release_slot`. Ambiguous create/readiness/cleanup holds the slot `held_uncertain`.
- provision_and_deploy: cleaned attempt -> `plan_next_attempt` (same provider once, then the other provider if the cap and availability allow,
  then one `provision.exhausted:<op>` incident and stop). Uncertain: stop, no retry, no fallback.
- `lease_keeper` renews the lease every `lease_renewal_seconds` inside the long actions; 2 consecutive failed renewals (or `lease_lost`) stop
  them. `assert_lease` runs inside each side-effecting action immediately before the adapter call.
- Policies: `capacity_guard` (one `capacity_guard_<kind>.yaml` per capacity kind, per `app_key`, delay), `heavy` (`cdr.run_heavy`, per `resource_class`, delay), `reconcile_single`
  (`cdr.reconcile`, cancel). The worker pool is set in `st2.docker.conf`.

Adapters (`actions/lib/providers/`): `provider_adapter` is required (`simulation` or `real`, no default). `real` is described under P5.2 below; its deploy runs Ansible (P5.3, below); readiness, routing and drain report `not_implemented` until P6.
`simulation` keeps `state.json` and `calls.jsonl` under `simulation_state_dir` (the `cdr_sim` volume at `/opt/stackstorm/cdr-sim`; it must be writable
by the st2 user) with `flock`; ids are `sim-<provider>-<n>`. Faults come from the optional file `simulation_faults` (JSON, re-read on every call, keyed
by provider or `"*"`; none are enabled by default): `never_ready`, `ready_after_s`, `create_timeout` (resource is created, call reports
`timeout_ambiguous`), `api_unknown`, `unavailable`, `create_latency_s`. `metrics_fresh` in `state.json` is the simulated CPU-signal freshness.
Instances reserved while the adapter is the simulation are written `simulated=true`; nothing here touches a cloud.

Tests: `pytest tests -q` (no skips when `TEST_DATABASE_URL` and `KEEPALIVE_TEST_URL` are set; the `restore_probe` tests also need `pg_dump`/`pg_restore` of the server's major version on PATH and fail, not skip, without them). Extra test dependencies: `pyyaml`, and for
`test_workflow_failure_paths.py` Orquesta itself (st2 3.9 ships 1.6.0, which is not on PyPI: `pip install --no-deps` the GitHub `v1.6.0` tag plus
`networkx==3.1 yaql jinja2 six stevedore ujson jsonschema chardet`). That test drives the real workflow YAML through Orquesta's conductor and
checks that every failure or no-op branch ends in `fail_operation`/`release_lease` with the held generation, and that each task input matches the
action's declared parameter types (st2 rejects a mismatch before the action starts).
Live check on a local st2 container: `tests/compose/t08-t09-local.sh` (output `tests/compose/t08-t09.local.out`, labelled local simulation).
It asserts that no live lease or open operation exists before each test, that the capacity guard did not delay the runs, and runs T09b (the
database paused for 25 s while `scale_out` holds the lease: the operation must end terminal and the lease must be free). On any FAIL it writes
evidence to `tests/compose/evidence/<UTC timestamp>/NN-label/` (gitignored): every `cdr.*` execution with result/stdout/stderr, lease, slots,
operations, attempts, incidents, events, simulation state and call log, runner/scheduler/workflow-engine logs. `T08_SKIP_T08`, `T08_SKIP_T09`,
`T08_SKIP_BLIP`, `T08_KEEP` and `T08_FORCE_FAIL` select parts of the run.

## Reconcile, failover and scale-in (P4.3, migration `ops/0012`) - local simulation only

Workflows (`actions/workflows/`, ref = operation kind): `cdr.reconcile`, `cdr.primary_failure`, `cdr.scale_in`, `cdr.activate_standby`; `cdr.scale_out`
and `cdr.provision_and_deploy` gained `resume`, `first_attempt`, `attempts_done`. `cdr.standby_repair` is implemented in P4.4 (below). The full
behaviour (adoption, resume by operation_id, tag scan, orphans, primary outage refusal, drain timeout, lease-store outage) is in docs/07, "Resolved
engineering defaults (P4.3)"; docs/15 section 1 is the operator side.

- Triggers: rule `reconcile_every_60s` (IntervalTimer 60 s; overlapping ticks are cancelled by `reconcile_single`, keyed on `app_key`), the init-profile compose
  service `st2-startup-reconcile` (once, after registration), and `cdr_alert` -> `cdr.alert_intent` for `app.cpu.high` (activate the idle warm standby first,
  then scale out), `app.cpu.low`, `app.primary.unhealthy` and `metrics.stale` (flag that blocks CPU-driven intents and scale-in).
- New actions: `reconcile_pass` (the whole pass; `lease_generation=0` is the read-only observe pass), `wait_lease`, `apply_routing` and `drain_instance`
  (SIMULATED; the real adapter reports `not_implemented` until P6), `scale_in_gate`, `failover_target`, `delete_slot_resource`, `adopt_attempt`, `supersede_queued`,
  `control_state`, `resolve_incident`, `dispatch`, `alert_intent`. Shared code: `lib/st2_api.py` (execution lookup, cancel, dispatch through `ST2_ACTION_API_URL` and
  `ST2_ACTION_AUTH_TOKEN`, no key in the pack config), `lib/cleanup.py`, `lib/metrics.py`.
- Simulation additions: `post_create_latency_s` (persist the resource, then stall the create call: a kill lands "created, not bound"), `drain_busy`, a `routing`
  map in `state.json`, and `SimulationAdapter.inject` (a project-tagged resource nobody asked for; test helper only).
- Pytest: `test_p43_actions.py` (real Postgres, real simulation adapter, fake StackStorm API), `test_workflow_p43.py` (Orquesta conductor over the real YAML).
  Live check: `tests/compose/t10-t22-local.sh` (timer, T10, T10b fencing, T22, restart gate, orphan gate; `T10_ONLY="t22 orphan"` selects; output
  `tests/compose/t10-t22.local.out`, labelled local simulation). `tests/compose/lib-st2-local.sh` is the bring-up shared with `t08-t09-local.sh`, which now
  disables the 60 s timer rule so a reconcile tick cannot change state mid-assertion (`LOCAL_TIMER_ON=1` leaves it on).

## Standby repair, apply_policy and the skeletons (P4.4, migration `ops/0013`) - local simulation only

Behaviour is in docs/04 (warm standby repair, "As implemented") and docs/07 ("Resolved engineering defaults (P4.4)"); this is the map.

- `cdr.standby_repair` (workflow): lease -> `standby_classify` -> routing `none` -> (a) redeploy, (b) `provider_power_on` + redeploy, (c) release with absence evidence + reserve `warm` + `provision_and_deploy slot_kind=warm`,
  (d) hold + incident `standby.uncertain:<slot>` + interim warm on the other provider (`slot_kind=interim_warm`, only while it answers) -> routing `backup` -> `standby_verify` -> `standby.repaired`. One retry of a missed readiness deadline.
  `standby_window` supplies the readiness window of a redeploy. `cdr.standby_unhealthy` is the SIMULATION producer of `standby.unhealthy` (refuses unless `provider_adapter: simulation`; `sim_fault` none | app_broken | powered_off | absent).
- Simulation additions: per-resource `power` and `app_broken` (`SimulationAdapter.set_resource`), `power_on`, `pinned_digest`, the fault `prometheus_reload_fail`, and the simulated Prometheus files (`<simulation_state_dir>/prometheus/prometheus.yml` and `prometheus.loaded.yml`).
- `cdr.apply_policy` (workflow): `policy_validate` -> (lease only when capacity limits change) -> `policy_compile` -> `policy_verify` -> `policy_activate`; any failure `policy_rollback` then `fail_operation`. Pack settings `policy_compile` (`simulation` | `real`, required, no default),
  `prometheus_config_path` (real). `lib/prom_render.py` (stdlib; `tests/compose/promtool.sh` uses it), `lib/prom_target.py`, `lib/policy_scope.py` (what a change touches; HAProxy, k6, Prometheus rule thresholds and StackStorm timers fail `not_implemented`).
- Skeletons `start_load_test`, `stop_load_test`, `create_backup`, `verify_backup`, `restore_application`: every planned step is a `cdr.not_implemented` task; the first fails the operation with `outcome.error: not_implemented` and the step; no lease, slot, routing or provider call.
- Reconcile additions: standby repair and surplus-interim scale-in intents (`intents`), dead or stale `apply_policy` (`policy_failed`).
- Per-kind capacity guards now cover `standby_repair` and `apply_policy`; the heavy wrapper is unchanged.
- Tests: `tests/test_p44_actions.py` (real Postgres, real simulation adapter, fake StackStorm API), `tests/test_workflow_p44.py` (Orquesta conductor over the real YAML, including the skeletons). Live: `tests/compose/t27-t36-local.sh` (T27 a-d, T36, apply_policy, skeletons;
  output `tests/compose/t27-t36.local.out`, labelled local simulation). `tests/compose/README.md` lists every Phase 4 test with its script and assertion.

## Real adapter and Terraform through run_heavy (P5.2) - code and mocked tests only

Behaviour is in docs/07 ("Real adapter (P5.2)"); settings in docs/12. Map:

- `lib/providers/real.py` (`RealAdapter`), `http_api.py` (DigitalOcean/Linode get, list_by_tag, observe, power_on), `tf_runner.py` (runs `scripts/tf-slot.sh`, reads state files). Tokens come from environment variables named in `real_adapter.*.token_env`.
- `cdr.run_heavy` tasks: `deploy`, `terraform_apply` (`exists` true create / false delete), `terraform_output`, `ansible_deploy` (`not_implemented`, P5.3). It is the only Terraform path; `provision_attempt` creates through it and runs `tf_delete` before `provider_cleanup`, `scale_in` before `delete_slot_resource`. In simulation, `terraform_apply exists=true` is the simulated create and `exists=false` changes nothing.
- `lib/cleanup.py: absence_evidence` is the single builder of release evidence: real mode needs zero resources in the slot's state AND provider `get` absent AND an empty `list_by_tag`; anything else keeps the slot held.
- Config: the `real_adapter` object (all keys required once present). Extra test dependencies: `responses==0.25.3`, `jsonschema`. `requests==2.32.3` is a runtime requirement.
- Tests: `tests/test_real_adapter.py` (`responses` for the provider APIs; the real `tf-slot.sh` driving `tests/fixtures/fake-terraform.sh`, which writes a state file; run_heavy fenced against real Postgres). No real API call and no real Terraform run.

## Catalogue

Workflows (Orquesta, `actions/workflows/`, ref = operation kind unless noted):

| Ref | Role | Phase |
| --- | --- | --- |
| `cdr.scale_out`, `cdr.scale_in`, `cdr.primary_failure`, `cdr.activate_standby`, `cdr.reconcile` | capacity operations (docs/04, docs/07) | P4.2, P4.3 |
| `cdr.standby_repair` | warm standby repair, interim warm | P4.4 |
| `cdr.apply_policy` | policy activation (Prometheus label, caps) | P4.4 (rest P6) |
| `cdr.provision_and_deploy`, `cdr.provision_attempt` | sub-workflows (not operation kinds in the API except provision_and_deploy) | P4.2 |
| `cdr.failback` | real adapter: probes, weight 0, ready, ramp 10/50/100 gated on PromQL; simulation: fails `not_implemented` at `direct_probes` before any lease | P6.2 |
| `cdr.start_load_test`, `cdr.stop_load_test` | skeleton, fails `not_implemented` | P6/P8 |
| `cdr.create_backup`, `cdr.verify_backup`, `cdr.restore_application` | skeleton, fails `not_implemented` | P7 |

Actions (python-script, `actions/*.py`):

| Group | Actions |
| --- | --- |
| Lease | `acquire_lease`, `wait_lease`, `renew_lease`, `assert_lease`, `release_lease` |
| Slots and instances | `reserve_slot`, `bind_slot`, `begin_slot_release`, `release_slot`, `mark_slot_uncertain`, `set_instance_state`, `next_elastic_provider`, `failover_target`, `scale_in_gate`, `capacity_gate` |
| Provisioning | `start_attempt`, `provider_create`, `adopt_attempt`, `run_heavy`, `wait_ready`, `finish_attempt`, `provider_cleanup`, `plan_next_attempt`, `delete_slot_resource` |
| Standby | `standby_classify`, `standby_window`, `standby_verify`, `provider_power_on`, `standby_unhealthy` (simulation producer) |
| Policy | `policy_validate`, `policy_compile`, `policy_verify`, `policy_activate`, `policy_rollback` |
| Routing and drain (simulation: projection only; real: HAProxy Runtime API, P6.2) | `apply_routing`, `drain_instance` |
| Failback and baseline (real adapter only) | `failback_probe`, `failback_wait_up`, `failback_observe`, `capture_baseline` |
| Operations and incidents | `open_operation`, `update_ops_state`, `fail_operation`, `open_incident`, `resolve_incident`, `supersede_queued`, `control_state`, `not_implemented` |
| Reconcile and intents | `reconcile_pass`, `dispatch`, `alert_intent`, `record_alert` |
| Other | `adapter_info`, `keepalive` |

Policies (`policies/`): `capacity_guard.{scale_out,scale_in,primary_failure,activate_standby,standby_repair,apply_policy}` (`app_key`, threshold 1, delay), `heavy` (`cdr.run_heavy` per `resource_class`, delay), `reconcile_single` (`cdr.reconcile`, cancel).
Rules (`rules/`): `cdr_alert` (Alertmanager webhook -> `cdr.alert_intent`), `reconcile_every_60s`, `keepalive_every_6h`.
Libraries (`actions/lib/`): `cdr_db`, `cdr_common`, `lease_keeper`, `cleanup`, `metrics`, `st2_api`, `alert_parse`, `prom_render`, `prom_target`, `policy_scope`, `providers/{base,simulation,real,http_api,tf_runner}`.
Pack settings (`config.schema.yaml`): `supabase_ops_url`, `provider_adapter` (required), `policy_compile` (required), `real_adapter` (required for `provider_adapter: real`), `simulation_faults`, `simulation_state_dir`, `prometheus_url`, `prometheus_expected_jobs`, `prometheus_config_path`.

## ansible_deploy through run_heavy (P5.3) - code and fake-binary tests only

`cdr.run_heavy` tasks `deploy` and `ansible_deploy` (real adapter) look the instance up in `ops.instances` (provider + provider resource id, not simulated), then run `ansible-playbook playbooks/app.yml --limit <instance id>` (`actions/lib/providers/ansible_runner.py`) under the same lease check and LeaseKeeper as Terraform. Pack setting `real_adapter.ansible{playbook_dir, inputs_path, ssh_key_path, ansible_bin?}` (paths only). The environment is scrubbed: the ops database URL for `infra/ansible/inventory/cloud_app.py` plus only the secret names the deployment inputs reference. Results: `ok` (+ `state`, `identity`, `image_digest`) when the run exits 0 and the app role's facts match; otherwise `{result: failed, code: deploy_failed | deploy_timeout | deploy_unverified | instance_not_found}`, a failed attempt and never `timeout_ambiguous` (the VM exists). Tests use `tests/fixtures/fake-ansible-playbook.sh`; no host is contacted.

## Restore probe (T18 heavy half)

`cdr.restore_probe` (operator-started, never scheduled) opens a `reconcile`-kind operation (the kinds are fixed by `ops.open_operation`; a dedicated backup kind arrives with P7), takes the capacity lease and runs `cdr.run_heavy` task `restore_probe`: `pg_dump -Fc -n app` from `backup_db_url`, `pg_restore` into a scratch database on `restore_test_url` (the `restore-test` profile container), table sets and row counts compared, scratch database dropped, dump deleted. Credentials go to the tools as `PG*` variables of a minimal environment, never argv; only short codes and counts are returned. It never touches the live app schema and is not the P7 restore.

## Real HAProxy routing adapter (P6.2)

- `lib/haproxy_admin.py` (mTLS client, allowlist mirror, `show servers state|conn` parsers, membership store), `lib/haproxy_routing.py` (modes, ramp weights, server-line mapping, fenced `Router`), `lib/routing_bind.py` (assert_lease and `ops.record_haproxy_command` hooks), `lib/promql.py` (failback gates), `lib/failback_lib.py`.
- Pack setting `real_adapter.haproxy` (`admin_addr`, `client_cert`, `client_key`, `ca_bundle`, `state_dir`; paths only). Without it the real adapter refuses routing. `prometheus_url` is also used by the failback gates.
- Tests: `tests/test_haproxy_admin.py` (fake TLS fe_admin; needs the `openssl` CLI and, for the audit tests, `TEST_DATABASE_URL`), `tests/test_workflow_failback.py`, `tests/test_live_lb_check.py`. The live read-only check against the real LB is `tests/live/live_lb_check.py` (run on the management VM inside `st2actionrunner`; it is not collected by pytest).
