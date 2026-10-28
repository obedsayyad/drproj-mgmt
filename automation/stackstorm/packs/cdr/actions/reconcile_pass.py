"""One reconcile pass (docs/04 Reconciliation, docs/07). Runs inside cdr.reconcile.

lease_generation = 0 is the read-only observe pass (another operation holds a live lease): it classifies and counts, writes
nothing. With the lease it:

  1. classifies every non-terminal capacity operation. A live StackStorm execution of the same operation_id whose lease
     is alive (or which is young and has not taken the lease yet) is ADOPTED: no new work. Otherwise it is dead: its
     executions are cancelled, the operation is marked reconciling, and it is either RESUMED (scale_out, primary_failure that
     hold a slot or can re-run: re-dispatched under the same operation_id, so start_attempt reports created=false and the
     attempt is adopted by tag scan, never created again) or failed `interrupted`. A queued intent nobody runs is closed
     as a succeeded no-op `stale_intent`. A StackStorm API answer of "unknown" never triggers action.
  2. scans the provider by project tag. Slots held_uncertain (or reserved by a dead operation) resolve with provider-API
     evidence: found -> bound, listed-and-absent -> free. A releasing slot of a dead operation is freed only on confirmed
     absence, else an incident. A project-tagged resource that no slot or live operation explains is NEVER deleted: its
     slot is held_uncertain when it is free (counted) and an `orphan:<provider>:<id>` incident is opened.
     A provider that does not answer is skipped; one with no token (provider_unconfigured) is skipped too and opens ONE
     `provider.unconfigured:<provider>` incident (fingerprint-deduped); with both unconfigured the pass reports noop_reason=providers_unconfigured.
  3. (P4.4) opens the standby intents the state calls for: `standby_repair` when the warm slot is bound but its instance is failed/unknown, or the warm slot
     is still held_uncertain with no interim warm standby; `scale_in` of the surplus interim-warm slot once the warm slot is bound and ready again. At most one per
     fingerprint and backoff (twice the lease length) since the last one; the workflows re-check everything under the lease. A dead `apply_policy` fails its
     pending policy version (the old version stays active) and puts the previous policy_version label back.
"""
import providers
from cdr_common import advance, guarded, policy_section
from cdr_db import call
from haproxy_admin import AdminError
from haproxy_routing import expected_effect
from cleanup import absence_evidence
from lease_keeper import LeaseKeeper, LeaseLost
from providers.base import PROJECT_TAG, now_iso
from st2_api import LIVE_STATUSES, St2Api, St2Unavailable
from st2common.runners.base_action import Action

CAPACITY_KINDS = ("scale_out", "scale_in", "primary_failure", "activate_standby", "standby_repair", "reconcile", "apply_policy")
RESUMABLE = ("scale_out", "primary_failure")
TERMINAL = ("succeeded", "failed", "cancelled")


class ReconcilePass(Action):
    def _api(self):
        return St2Api()

    @guarded
    def run(self, operation_id, lease_generation=0, previous_holder=None, app_key="relief"):
        adapter = providers.load(self.config)
        ok, coord = policy_section(self.config, operation_id, "coordination")
        if not ok:
            return False, coord
        self.observe = not lease_generation
        self.op, self.gen, self.app = operation_id, lease_generation, app_key
        self.adapter = adapter
        self.summary = {"observe_only": self.observe, "adopted": [], "stale_queued": [], "interrupted": [], "resumes": [], "resolved_slots": [],
                        "released_slots": [], "orphans": [], "providers_skipped": [], "providers_unconfigured": [], "st2_unknown": [], "errors": [], "intents": [], "policy_failed": []}
        try:
            api = self._api()
        except St2Unavailable:
            api = None
        if self.observe:
            return self._passes(coord, previous_holder, api)
        with LeaseKeeper(self.config, operation_id, lease_generation, coord["lease_renewal_seconds"]) as keeper:
            self.keeper = keeper
            return self._passes(coord, previous_holder, api)

    # ---- helpers
    def _q(self, sql, params=()):
        ok, rows = call(self.config, sql, params)
        if not ok:
            raise _Db(rows)
        return rows

    def _w(self, sql, params=()):
        """A write under the lease; lease_lost aborts the pass, other failures are reported and the pass goes on."""
        if self.observe:
            return None
        self.keeper.check()
        ok, rows = call(self.config, sql, params)
        if ok:
            return rows
        if rows == "lease_lost":
            raise LeaseLost("lease_lost")
        self.summary["errors"].append(rows)
        return None

    def _passes(self, coord, previous_holder, api):
        try:
            handled = self._operations(coord, previous_holder, api)
            self._providers(handled)
            if not self.observe:
                self._haproxy_backfill()
                self._standby_intents(coord)
        except _Db as exc:
            return False, exc.code
        return True, self.summary

    # ---- 1. operations
    def _operations(self, coord, previous_holder, api):
        interval, lease_s = coord["reconcile_interval_seconds"], coord["lease_seconds"]
        ops = self._q(
            "SELECT id::text, kind, status, stackstorm_execution_id, extract(epoch FROM now() - created_at)::int, extract(epoch FROM now() - updated_at)::int"
            " FROM ops.operations WHERE status NOT IN ('succeeded', 'failed', 'cancelled') AND id <> %s::uuid AND kind = ANY(%s) ORDER BY created_at",
            (self.op, list(CAPACITY_KINDS)),
        )
        lease = self._q("SELECT holder_operation::text, lease_expires_at > clock_timestamp() FROM ops.control_leases WHERE application_key = %s", (self.app,))
        holder, lease_live = (lease[0][0], bool(lease[0][1])) if lease else (None, False)
        handled = {"live": set(), "resumed": set(), "dead": set()}
        for op, kind, status, exec_id, created_age, idle in ops:
            if status == "paused":
                handled["live"].add(op)  # paused for an operator (drain timeout): its slots are not ours to resolve
                continue
            try:
                execs = self._live_execs(api, kind, op, exec_id)
            except St2Unavailable:
                self.summary["st2_unknown"].append(op)
                handled["live"].add(op)
                continue
            if status == "queued":
                if execs or created_age <= 2 * interval:
                    self.summary["adopted"].append(op)
                    handled["live"].add(op)
                elif kind == "apply_policy":
                    self.summary["stale_queued"].append(op)  # never started: its pending version must not block the next change forever
                    self._fail_policy(op, "stale_intent")
                    self._w_advance(op, "operation.started", "stale_intent", "running", "policy apply never started")  # queued -> running -> failed
                    self._w_advance(op, "operation.failed", "stale_intent", "failed", "policy apply never started; the old version stays active", {"error": "stale_intent"})
                else:
                    self.summary["stale_queued"].append(op)
                    self._w_advance(op, "operation.succeeded", "noop", "succeeded", "no action - stale_intent", {"noop_reason": "stale_intent"})
                continue
            holds = holder == op
            stale = previous_holder == op or (holds and not lease_live) or idle >= 3 * lease_s
            if execs and not stale and (holds and lease_live or not holds):
                self.summary["adopted"].append(op)
                handled["live"].add(op)
                continue
            self._dead(op, kind, status, execs, api, handled)
        return handled

    def _heavy_children(self, api, op):
        """Live cdr.run_heavy executions of the dead worker's operation. A SIGKILLed runner leaves its run_heavy `running` for ever, and the heavy
        policy (action.concurrency.attr, threshold 1) counts scheduled + running, so the resumed run_heavy would be `delayed` behind that ghost (found by the
        P6.2 restart gate: tests/compose/t10-t22-local.sh). Cancelling moves it to `canceling`, which the policy does not count."""
        try:
            return sorted(r["id"] for r in api.find_live("cdr.run_heavy", op))
        except St2Unavailable:
            return []

    def _live_execs(self, api, kind, op, exec_id):
        if api is None:
            raise St2Unavailable("st2_api_unavailable")
        ids = {r["id"] for r in api.find_live(f"cdr.{kind}", op)}
        if exec_id:
            doc = api.execution(exec_id)
            if doc is not None and doc.get("status") in LIVE_STATUSES:
                ids.add(exec_id)
        return sorted(ids)

    def _w_advance(self, op, event, step, status, message, outcome=None):
        if self.observe:
            return
        ok, out = advance(self.config, op, event, step, status, message, outcome, source="reconciliation")
        if not ok and out != "invalid_transition":
            self.summary["errors"].append(out)

    def _dead(self, op, kind, status, execs, api, handled):
        handled["dead"].add(op)
        if self.observe:
            self.summary["interrupted"].append(op)
            return
        if status in ("running", "waiting", "verifying"):
            self._w_advance(op, "operation.step.completed", "reconcile_takeover", "reconciling", "worker gone; reconcile took over")
        for eid in execs + self._heavy_children(api, op):
            try:
                api.cancel(eid)
            except St2Unavailable:
                pass
        attempts = self._q("SELECT attempt, status, provider FROM ops.operation_attempts WHERE operation_id = %s::uuid ORDER BY attempt", (op,))
        held = self._q("SELECT key, generation, provider, state FROM ops.capacity_slots WHERE lease_operation_id = %s::uuid AND state IN ('reserved', 'bound') ORDER BY key LIMIT 1", (op,))
        if kind in RESUMABLE and (held or kind == "primary_failure") and status != "cancelling":
            latest = attempts[-1] if attempts else None
            first = 1 if latest is None else latest[0] if latest[1] in ("started", "failed", "ready") else latest[0] + 1
            done = {}
            for _n, st, prov in attempts:
                if st == "cleaned" and prov:
                    done[prov] = done.get(prov, 0) + 1
            params = {"operation_id": op, "resume": True, "first_attempt": first, "attempts_done": done}
            self.summary["resumes"].append({"ref": f"cdr.{kind}", "parameters": params})
            handled["resumed"].add(op)
            return
        self.summary["interrupted"].append(op)
        if kind == "apply_policy":
            self._fail_policy(op, "interrupted")
        for n, st, _prov in attempts:
            if st == "started":
                self._attempt(op, n, "failed", "interrupted")
        if held and held[0][3] == "reserved":
            key, gen = held[0][0], held[0][1]
            self._w("SELECT ops.mark_slot_uncertain(%s::uuid, %s, %s, %s, 'operation_interrupted', %s)", (self.op, self.gen, key, gen, self.app))
        self._w_advance(op, "operation.failed", "interrupted", "failed", "interrupted; reconcile resolved what it could", {"error": "interrupted"})

    def _fail_policy(self, op, reason):
        """A dead apply_policy: fail its pending version (old stays active) and re-render the previous label (best effort, reported)."""
        rows = self._w("SELECT ops.fail_pending_policy(%s::uuid, %s, %s::uuid, %s, %s)", (self.op, self.gen, op, reason, self.app))
        if not rows or not rows[0][0]:
            return
        self.summary["policy_failed"].append(op)
        try:
            from prom_render import set_policy_version
            from prom_target import load

            active = self._q("SELECT version FROM ops.policy_versions WHERE status = 'active'")
            target = load(self.config)
            target.write(set_policy_version(target.read(), active[0][0]))
            target.reload()
        except Exception:  # noqa: BLE001 - best effort: Prometheus may be unconfigured or down; the state above is what matters
            self.summary["errors"].append("policy_label_not_restored")

    def _attempt(self, target, attempt, status, error=None):
        self._w("SELECT ops.reconcile_attempt(%s::uuid, %s, %s::uuid, %s, %s, %s, %s)", (self.op, self.gen, target, attempt, status, error, self.app))

    # ---- 2. providers
    def _providers(self, handled):
        slots = {r[0]: r for r in self._q(
            "SELECT c.key, c.generation, c.provider, c.state, c.lease_operation_id::text, i.provider_resource_id, i.simulated"
            " FROM ops.capacity_slots c LEFT JOIN ops.instances i ON i.id = c.bound_instance_id ORDER BY c.key")}
        keep_ops = handled["live"] | handled["resumed"]
        listings = {}
        for provider in ("digitalocean", "linode"):
            res = self.adapter.list_by_tag(provider, dict(PROJECT_TAG))
            if res["state"] == "ok":
                listings[provider] = res["resources"]
            else:
                self.summary["providers_skipped"].append(provider)
                if res.get("code") == "provider_unconfigured":
                    self.summary["providers_unconfigured"].append(provider)
                    self._incident(f"provider.unconfigured:{provider}", "warning",
                                   f"{provider} has no API token configured; reconcile skips its tag scan and cannot see or resolve its resources."
                                   " Set the token (docs/15) and the next pass scans it", [])
        if len(self.summary["providers_unconfigured"]) == 2:
            self.summary["noop_reason"] = "providers_unconfigured"
        explained = {r[5] for r in slots.values() if r[3] != "free" and r[5]}
        for key, (_k, gen, provider, state, owner, rid, _sim) in sorted(slots.items()):
            if provider not in listings or owner in keep_ops:
                continue
            self.keeper_check()
            if state == "reserved" and owner in handled["dead"]:
                self._w("SELECT ops.mark_slot_uncertain(%s::uuid, %s, %s, %s, 'operation_interrupted', %s)", (self.op, self.gen, key, gen, self.app))
                state = "held_uncertain"
            if state == "held_uncertain":
                self._resolve_held(key, gen, provider, owner, listings[provider], explained)
            elif state == "releasing":
                self._resolve_releasing(key, gen, provider, rid)
        for provider, resources in listings.items():
            for r in resources:
                rid, tags = r["resource_id"], r.get("tags") or {}
                if rid in explained or (tags.get("op") in keep_ops and tags.get("slot") in slots and slots[tags["slot"]][4] == tags.get("op")):
                    continue
                self._orphan(provider, r, slots)

    # ---- 2b. HAProxy command intents (P6.2-FIX-1)
    PENDING_GRACE_SECONDS = 30

    def _haproxy_backfill(self):
        """Close `pending` ops.haproxy_commands rows left by a dead worker. The change stands either way (no rollback). A row is `applied`
        when a stored dump taken since the intent (else the live membership) shows the command's effect, `abandoned` when neither does
        (never sent, or superseded by a later command with no dump in between). Live LB unreachable: the row stays pending for the next pass."""
        if self.adapter.simulated or getattr(self.adapter, "store", None) is None:
            return
        rows = self._q("SELECT id::text, command, to_char(created_at AT TIME ZONE 'UTC', 'YYYYMMDD\"T\"HH24MISSUS\"Z\"') FROM ops.haproxy_commands"
                       " WHERE status = 'pending' AND created_at < now() - make_interval(secs => %s) ORDER BY created_at", (self.PENDING_GRACE_SECONDS,))
        live = None
        for cid, command, stamp in rows:
            self.keeper_check()
            cmd = command + "\n"
            hit = next(((n, m) for n, m in self.adapter.store.since(stamp) if expected_effect(cmd, m)), None)
            if hit is None:
                try:
                    if live is None:
                        text, member = self.adapter.admin.show_state()
                        live = (self.adapter.store.save(text), member)
                except (AdminError, AttributeError):
                    self.summary["errors"].append("haproxy_backfill_lb_unreachable")
                    return
                hit = live if expected_effect(cmd, live[1]) else None
            if hit is None:
                done = self._w("SELECT ops.backfill_haproxy_command(%s::uuid, %s, %s::uuid, false, NULL, NULL, %s)", (self.op, self.gen, cid, self.app))
            else:
                server = hit[1].servers.get(command.split()[2].split("/", 1)[1])
                new = None if server is None else {"addr": server.addr, "port": server.port, "weight": server.weight, "op_state": server.op_state, "admin_state": server.admin_state}
                done = self._w("SELECT ops.backfill_haproxy_command(%s::uuid, %s, %s::uuid, true, %s, %s::jsonb, %s)", (self.op, self.gen, cid, hit[0], _json(new), self.app))
            if done:
                self.summary.setdefault("haproxy_backfilled", []).append({"id": cid, "status": "abandoned" if hit is None else "applied"})

    # ---- 3. standby intents (P4.4)
    def _intent(self, kind, fingerprint, event, message, backoff):
        """Open (at most once per backoff) the operation for an intent and queue its dispatch for after the lease is released."""
        if self._q("SELECT 1 FROM ops.operations WHERE kind = %s AND status NOT IN ('succeeded', 'failed', 'cancelled') LIMIT 1", (kind,)):
            return  # one in flight: it re-checks everything itself
        if self._q(
            "SELECT 1 FROM ops.processed_events e JOIN ops.operations o ON o.id = e.operation_id"
            " WHERE e.fingerprint = %s AND o.created_at > now() - make_interval(secs => %s) LIMIT 1", (fingerprint, backoff)):
            return
        rows = self._w("SELECT operation_id::text, created FROM ops.open_operation(%s, 'reconciliation', %s, %s)", (kind, fingerprint, event))
        if not rows or not rows[0][1]:
            return
        op = rows[0][0]
        self._w_advance(op, event, "trigger", None, message)
        self.summary["intents"].append({"kind": kind, "operation_id": op, "fingerprint": fingerprint})
        self.summary["resumes"].append({"ref": f"cdr.{kind}", "parameters": {"operation_id": op}})

    def _standby_intents(self, coord):
        backoff = 2 * coord["lease_seconds"]
        rows = self._q(
            "SELECT s.key, s.state, s.generation, i.lifecycle_state FROM ops.capacity_slots s LEFT JOIN ops.instances i ON i.id = s.bound_instance_id"
            " WHERE s.role = 'warm' ORDER BY s.key")
        warm = next((r for r in rows if r[0] == "warm"), None)
        interim = [r for r in rows if r[0] != "warm" and r[1] == "bound"]
        if warm is None:
            return
        key, state, generation, lifecycle = warm
        if (state == "bound" and lifecycle in ("failed", "unknown")) or (state == "held_uncertain" and not interim):
            self._intent("standby_repair", f"reconcile.warm_mismatch:{key}:{generation}", "standby.unhealthy",
                         f"warm slot {key} is {state} (instance {lifecycle or 'none'}); reconcile requests a repair", backoff)
        elif state == "bound" and lifecycle == "ready" and interim:
            self._intent("scale_in", f"reconcile.surplus_warm:{interim[0][0]}", "reconcile.requested",
                         f"{interim[0][0]} is the surplus warm-role slot now that {key} is bound and ready; reconcile requests its release", backoff)

    def keeper_check(self):
        if not self.observe:
            self.keeper.check()

    def _resolve_held(self, key, gen, provider, owner, resources, explained):
        matches = [r for r in resources if (r.get("tags") or {}).get("slot") == key]
        if not matches:
            ev, _why = absence_evidence(self.adapter, provider, key, [], by="reconcile")
            if ev is None:
                return  # absence not proven (real adapter): the slot stays held_uncertain
            if self._w("SELECT ops.resolve_held_slot(%s::uuid, %s, %s, %s, 'free', %s::jsonb, NULL, NULL, %s)", (self.op, self.gen, key, gen, _json(ev), self.app)) is not None:
                self.summary["resolved_slots"].append({"slot": key, "outcome": "free"})
                self._close_attempts(owner, key, "cleaned")
            return
        known = None
        if len(matches) == 1:
            tags = matches[0]["tags"]
            hit = self._q("SELECT 1 FROM ops.operation_attempts WHERE operation_id::text = %s AND attempt::text = %s AND slot_key = %s", (tags.get("op"), tags.get("attempt"), key))
            known = matches[0] if hit else None
        if known is None:
            return  # several resources or tags no attempt explains: reported as orphans below, the slot stays held
        ev = {"exists": True, "source": "provider_api", "observed_at": now_iso(), "simulated": self.adapter.simulated, "by": "reconcile", "resource_id": known["resource_id"]}
        if self._w("SELECT ops.resolve_held_slot(%s::uuid, %s, %s, %s, 'bound', %s::jsonb, %s, %s, %s)", (self.op, self.gen, key, gen, _json(ev), known["resource_id"], known.get("ip"), self.app)) is not None:
            self.summary["resolved_slots"].append({"slot": key, "outcome": "bound", "resource_id": known["resource_id"]})
            explained.add(known["resource_id"])
            self._close_attempts(owner, key, "failed")

    def _close_attempts(self, owner, key, target):
        """Settle the attempt rows of the slot's creating operation: uncertain/started -> failed (-> cleaned when the VM is absent)."""
        if not owner:
            return
        for n, st in self._q("SELECT attempt, status FROM ops.operation_attempts WHERE operation_id = %s::uuid AND slot_key = %s ORDER BY attempt", (owner, key)):
            if st in ("started", "uncertain"):
                self._attempt(owner, n, "failed", "adopted_by_reconcile" if target == "failed" else "absent_confirmed")
                st = "failed"
            if st == "failed" and target == "cleaned":
                self._attempt(owner, n, "cleaned", "absent_confirmed")

    def _resolve_releasing(self, key, gen, provider, rid):
        if not rid:
            return
        state = self.adapter.get(provider, rid)["state"]
        if state == "absent":
            ev, _why = absence_evidence(self.adapter, provider, key, [rid], by="reconcile")
            if ev is None:
                return
            if self._w("SELECT ops.release_slot(%s::uuid, %s, %s, %s, %s::jsonb, %s)", (self.op, self.gen, key, gen, _json(ev), self.app)) is not None:
                self.summary["released_slots"].append(key)
        elif state == "present":
            self._incident(f"release.pending:{key}", "warning", f"slot {key} is releasing but {rid} still exists; a deletion was interrupted. Reconcile does not delete; see docs/15 section 1", [key, rid])

    def _orphan(self, provider, r, slots):
        rid, tags = r["resource_id"], r.get("tags") or {}
        slot = tags.get("slot")
        row = slots.get(slot)
        held = False
        if row and row[3] == "free" and row[2] == provider:
            if self._w("SELECT ops.hold_orphan_slot(%s::uuid, %s, %s, %s, %s, %s, %s)", (self.op, self.gen, slot, provider, rid, bool(self.adapter.simulated), self.app)) is not None:
                held = True
        self.summary["orphans"].append({"provider": provider, "resource_id": rid, "slot": slot, "slot_held": held})
        self._incident(f"orphan:{provider}:{rid}", "critical",
                       f"project-tagged resource {rid} on {provider} (slot tag {slot or 'none'}, op tag {tags.get('op') or 'none'}) is explained by no slot or operation;"
                       " it is not deleted. Follow the 'Adopt tagged orphans' runbook (docs/15 section 1)", [rid] + ([slot] if slot else []))

    def _incident(self, fingerprint, severity, text, resources):
        # one clear incident for a recurring unresolved condition: an already open one gets no new event per tick
        if self._q("SELECT 1 FROM ops.incidents WHERE fingerprint = %s AND state = 'open'", (fingerprint,)):
            return
        self._w("SELECT incident_id::text FROM ops.open_incident(%s, %s, 'reconciliation', %s, %s::text[], %s::uuid)", (fingerprint, severity, text, resources, self.op))


class _Db(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _json(value):
    import json

    return json.dumps(value)
