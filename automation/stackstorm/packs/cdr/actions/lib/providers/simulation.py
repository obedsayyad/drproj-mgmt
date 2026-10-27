"""Local simulation adapter: a flock'd JSON file stands in for the provider APIs. Nothing here touches a cloud.

State dir (on the st2 volume): state.json (resources) and calls.jsonl (one line per adapter call, for tests).
Faults file (optional JSON, re-read on every call; absent or empty file = no faults), keyed by provider or "*":
  {"digitalocean": {"never_ready": true, "ready_after_s": 5, "create_timeout": true,
                    "api_unknown": true, "unavailable": true, "create_latency_s": 2}}
  never_ready      deploy succeeds, check_ready never reports ready
  ready_after_s    seconds after deploy until ready (default 5)
  create_timeout   the resource IS created, the call reports timeout_ambiguous
  api_unknown      get/list/delete/check_ready answer "unknown"
  unavailable      observe says unavailable; create refuses; get/list/delete answer unknown
  create_latency_s sleep before create (widens races)
  post_create_latency_s  sleep AFTER the resource is persisted and before the call returns (a kill lands "created, not bound")
  drain_busy       drain never completes (in-flight connections stay)
  prometheus_reload_fail  (key "*" or any provider) the simulated Prometheus reload of cdr.policy_compile fails
Per-resource faults (set with set_resource, not the faults file): power "off" (check_ready not ready, deploy refuses until power_on),
app_broken (check_ready not ready until the next deploy).
Routing and drain are simulated (`routing` map in state.json); HAProxy is P6.
`metrics_fresh` (top-level key of state.json, default true) is read by capacity_gate for the simulated CPU signal.
"""
import fcntl
import json
import os
import time

from .base import PINNED_DIGEST, ProviderAdapter, now_iso, tags_for

DEFAULT_READY_AFTER_S = 5


class SimulationAdapter(ProviderAdapter):
    simulated = True
    mode = "simulation"

    def __init__(self, state_dir, faults_path=None):
        self.state_dir = state_dir
        self.faults_path = faults_path
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        self._state_path = os.path.join(state_dir, "state.json")
        self._log_path = os.path.join(state_dir, "calls.jsonl")
        self._lock_path = os.path.join(state_dir, "lock")

    # ---- plumbing
    def faults(self, provider):
        if not self.faults_path:
            return {}
        try:
            with open(self.faults_path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return {**data.get("*", {}), **data.get(provider, {})}

    def _txn(self, fn, log=None):
        """Run fn(state) under an exclusive flock; persist state; append the call-log line in the same critical section."""
        with open(self._lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    with open(self._state_path) as f:
                        state = json.load(f)
                except (OSError, ValueError):
                    state = {}
                state.setdefault("seq", 0)
                state.setdefault("resources", {})
                result = fn(state)
                tmp = self._state_path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(state, f)
                os.replace(tmp, self._state_path)
                if log is not None:
                    line = {"ts": now_iso(), **log, "result": result.get("result", result.get("state")) if isinstance(result, dict) else result}
                    with open(self._log_path, "a") as f:
                        f.write(json.dumps(line) + "\n")
                return result
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def calls(self, method=None):
        try:
            with open(self._log_path) as f:
                rows = [json.loads(line) for line in f if line.strip()]
        except OSError:
            return []
        return [r for r in rows if method is None or r["method"] == method]

    def metrics_fresh(self):
        return bool(self._txn(lambda s: s.get("metrics_fresh", True)))

    def set_metrics_fresh(self, value):
        self._txn(lambda s: s.__setitem__("metrics_fresh", bool(value)))

    # ---- adapter API
    def create(self, provider, slot_key, operation_id, attempt, tags, spec):
        f = self.faults(provider)
        log = {"method": "create", "provider": provider, "slot": slot_key, "op": str(operation_id), "attempt": attempt}
        if f.get("create_latency_s"):
            time.sleep(float(f["create_latency_s"]))
        if f.get("unavailable"):
            return self._txn(lambda s: {"result": "unavailable"}, log)

        def do(s):
            s["seq"] += 1
            rid = f"sim-{provider}-{s['seq']}"
            s["resources"][rid] = {"provider": provider, "tags": dict(tags), "created": time.time(), "deployed": None,
                                   "ip": f"192.0.2.{s['seq'] % 250 + 1}", "power": "on", "deleted": False}
            if f.get("create_timeout"):
                return {"result": "timeout_ambiguous", "resource_id": None}
            return {"result": "ok", "resource_id": rid, "ip": s["resources"][rid]["ip"], "vcpu": 1, "memory_mb": 1024}

        res = self._txn(do, {**log, "created_resource": True})
        if f.get("post_create_latency_s"):
            time.sleep(float(f["post_create_latency_s"]))
        return res

    def tf_apply(self, provider, slot_key, operation_id, attempt, exists, spec=None):
        """The simulation has no Terraform: exists=true is the simulated create, exists=false changes nothing (the simulated delete happens in cleanup)."""
        if exists:
            return self.create(provider, slot_key, operation_id, attempt, tags_for(slot_key, operation_id, attempt), spec or {})
        return {"result": "ok", "note": "no_terraform_in_simulation"}

    def tf_output(self, provider, slot_key, spec=None):
        found = self.list_by_tag(provider, {"project": "cdr", "slot": slot_key})["resources"]
        return {"result": "ok", "resource_id": found[0]["resource_id"], "ip": found[0]["ip"]} if len(found) == 1 else {"result": "unknown"}

    def get(self, provider, resource_id):
        f = self.faults(provider)
        log = {"method": "get", "provider": provider, "resource_id": resource_id}
        if f.get("api_unknown") or f.get("unavailable"):
            return self._txn(lambda s: {"state": "unknown", "observed_at": now_iso()}, log)

        def do(s):
            r = s["resources"].get(resource_id)
            if r is None or r["deleted"] or r["provider"] != provider:
                return {"state": "absent", "observed_at": now_iso()}
            return {"state": "present", "power": r["power"], "ip": r["ip"], "observed_at": now_iso()}

        return self._txn(do, log)

    def delete(self, provider, resource_id):
        f = self.faults(provider)
        log = {"method": "delete", "provider": provider, "resource_id": resource_id}
        if f.get("api_unknown") or f.get("unavailable"):
            return self._txn(lambda s: {"result": "unknown"}, log)

        def do(s):
            r = s["resources"].get(resource_id)
            if r is not None and r["provider"] == provider:
                r["deleted"] = True
            return {"result": "ok"}

        return self._txn(do, log)

    def list_by_tag(self, provider, tags):
        f = self.faults(provider)
        log = {"method": "list_by_tag", "provider": provider}
        if f.get("api_unknown") or f.get("unavailable"):
            return self._txn(lambda s: {"state": "unknown", "resources": []}, log)

        def do(s):
            found = [{"resource_id": rid, "tags": r["tags"], "ip": r["ip"]} for rid, r in s["resources"].items()
                     if r["provider"] == provider and not r["deleted"] and all(r["tags"].get(k) == v for k, v in tags.items())]
            return {"state": "ok", "resources": found}

        return self._txn(do, log)

    def observe(self, provider):
        f = self.faults(provider)
        return "unavailable" if f.get("unavailable") else "unknown" if f.get("api_unknown") else "available"

    def deploy(self, provider, resource_id):
        log = {"method": "deploy", "provider": provider, "resource_id": resource_id}

        def do(s):
            r = s["resources"].get(resource_id)
            if r is None or r["deleted"] or r["power"] == "off":
                return {"result": "unknown"}
            r["deployed"] = time.time()
            r["app_broken"] = False
            return {"result": "ok"}

        return self._txn(do, log)

    def ansible_deploy(self, provider, resource_id, target=None, spec=None):
        return self.deploy(provider, resource_id)

    def check_ready(self, provider, resource_id):
        f = self.faults(provider)
        if f.get("api_unknown") or f.get("unavailable"):
            return {"state": "unknown"}

        def do(s):
            r = s["resources"].get(resource_id)
            if r is None or r["deleted"] or r["deployed"] is None or f.get("never_ready") or r["power"] == "off" or r.get("app_broken"):
                return {"state": "not_ready"}
            if time.time() - r["deployed"] < float(f.get("ready_after_s", DEFAULT_READY_AFTER_S)):
                return {"state": "not_ready"}
            return {"state": "ready", "image_digest": PINNED_DIGEST, "identity": resource_id}

        return self._txn(do)

    def power_on(self, provider, resource_id):
        f = self.faults(provider)
        log = {"method": "power_on", "provider": provider, "resource_id": resource_id}
        if f.get("api_unknown") or f.get("unavailable"):
            return self._txn(lambda s: {"result": "unknown"}, log)

        def do(s):
            r = s["resources"].get(resource_id)
            if r is None or r["deleted"] or r["provider"] != provider:
                return {"result": "unknown"}
            r["power"] = "on"
            return {"result": "ok"}

        return self._txn(do, log)

    def pinned_digest(self):
        return PINNED_DIGEST

    # ---- simulated Prometheus (cdr.policy_compile in simulation mode): prometheus.yml is the file, prometheus.loaded.yml what "reload" made live
    def prometheus_path(self):
        return os.path.join(self.state_dir, "prometheus", "prometheus.yml")

    def prometheus_loaded_text(self):
        try:
            with open(os.path.join(self.state_dir, "prometheus", "prometheus.loaded.yml")) as f:
                return f.read()
        except OSError:
            return None

    def prometheus_reload(self):
        fail = bool(self.faults("*").get("prometheus_reload_fail"))
        if not fail:
            with open(self.prometheus_path()) as f, open(os.path.join(self.state_dir, "prometheus", "prometheus.loaded.yml"), "w") as g:
                g.write(f.read())
        return self._txn(lambda s: {"result": "failed" if fail else "ok"}, {"method": "prometheus_reload"})["result"]

    # ---- routing and drain (simulated; the real HAProxy runtime API is P6)
    def set_routing(self, target, mode):
        return self._txn(lambda s: s.setdefault("routing", {}).__setitem__(target, mode) or {"result": "ok"},
                         {"method": "set_routing", "target": target, "mode": mode})

    def routing(self):
        return self._txn(lambda s: dict(s.get("routing", {})))

    def drain(self, provider, resource_id):
        """-> "drained" | "busy" | "unknown" """
        f = self.faults(provider)
        if f.get("api_unknown") or f.get("unavailable"):
            return self._txn(lambda s: {"state": "unknown"}, {"method": "drain", "provider": provider, "resource_id": resource_id})["state"]
        state = "busy" if f.get("drain_busy") else "drained"
        return self._txn(lambda s: {"state": state}, {"method": "drain", "provider": provider, "resource_id": resource_id})["state"]

    # ---- test helpers (not part of the adapter interface): break one resource, or inject a project-tagged resource nobody asked for
    def set_resource(self, resource_id, **fields):
        """Set per-resource fault fields (power, app_broken, deleted) on a simulated resource; False when it does not exist."""
        def do(s):
            r = s["resources"].get(resource_id)
            if r is None:
                return {"result": False}
            r.update(fields)
            return {"result": True}

        return self._txn(do, {"method": "set_resource", "resource_id": resource_id})["result"]

    def inject(self, provider, tags):
        def do(s):
            s["seq"] += 1
            rid = f"sim-{provider}-{s['seq']}"
            s["resources"][rid] = {"provider": provider, "tags": dict(tags), "created": time.time(), "deployed": None,
                                   "ip": f"192.0.2.{s['seq'] % 250 + 1}", "power": "on", "deleted": False}
            return {"result": rid}

        return self._txn(do, {"method": "inject", "provider": provider})["result"]
