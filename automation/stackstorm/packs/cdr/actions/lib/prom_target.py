"""Where a rendered prometheus.yml goes. `policy_compile` (pack config, required, no default) picks:

  simulation  a file under the simulation state dir; "reload" copies it to the "loaded" file unless the fault prometheus_reload_fail is set.
              Nothing real is touched; the seed file is a copy of the repo's prometheus.yml placed there by the test script.
  real        the live file `prometheus_config_path` (written atomically) and `prometheus_url` /-/reload, verified through
              /api/v1/status/config. Needs Prometheus started with --web.enable-lifecycle (compose wiring is P6, docs/11).
"""
import json
import os
import urllib.request

from prom_render import read_policy_version


class PolicyCompileConfigError(Exception):
    """policy_compile missing or unknown, or its real-mode settings absent."""


class SimTarget:
    mode = "simulation"

    def __init__(self, adapter):
        self.adapter = adapter
        self.path = adapter.prometheus_path()

    def read(self):
        with open(self.path) as f:
            return f.read()

    def write(self, text):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, self.path)

    def reload(self):
        return self.adapter.prometheus_reload() == "ok"

    def loaded_version(self):
        return read_policy_version(self.adapter.prometheus_loaded_text() or "")


class RealTarget:
    mode = "real"

    def __init__(self, path, url):
        self.path, self.url = path, url.rstrip("/")

    def read(self):
        with open(self.path) as f:
            return f.read()

    def write(self, text):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, self.path)

    def reload(self):
        try:
            req = urllib.request.Request(self.url + "/-/reload", method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - operator-configured URL
                return 200 <= r.status < 300
        except Exception:  # noqa: BLE001
            return False

    def loaded_version(self):
        try:
            with urllib.request.urlopen(self.url + "/api/v1/status/config", timeout=10) as r:  # noqa: S310
                return read_policy_version(json.load(r)["data"]["yaml"])
        except Exception:  # noqa: BLE001
            return None


def load(config):
    mode = (config or {}).get("policy_compile")
    if mode == "simulation":
        from providers.simulation import SimulationAdapter
        from providers import DEFAULT_SIM_DIR

        return SimTarget(SimulationAdapter(config.get("simulation_state_dir") or DEFAULT_SIM_DIR, config.get("simulation_faults") or None))
    if mode == "real":
        if not config.get("prometheus_config_path") or not config.get("prometheus_url"):
            raise PolicyCompileConfigError("policy_compile_real_needs_path_and_url")
        return RealTarget(config["prometheus_config_path"], config["prometheus_url"])
    raise PolicyCompileConfigError("policy_compile_missing_or_unknown")
