"""k6 on the testing host (docs/10, P6.3). The only channel is one SSH key whose authorized_keys entry forces /opt/drproj/k6/bin/k6ctl:
every call here is a fixed argv `ssh ... root@host <verb> <validated args>`, never a shell string from the API, and k6ctl validates the
same bounds again on the far side. `run` is injectable for tests."""
import json
import re
import subprocess

SCENARIOS = ("smoke", "baseline", "compute_pressure", "sustained_surge", "recovery")
RUN_ID_RE = re.compile(r"[A-Za-z0-9-]{1,40}")
HOST_RE = re.compile(r"[0-9.]{7,15}")
USER_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}")


class LoadTestError(Exception):
    pass


def check_bounds(demo, scenario, rate, duration, work_ms):
    """Policy demo.* bounds. Returns a short code, or None when inside them."""
    if scenario not in SCENARIOS:
        return "bad_scenario"
    if not (1 <= rate <= demo["max_rate_rps"]) or not (1 <= duration <= demo["max_duration_seconds"]) or not (1 <= work_ms <= demo["max_work_ms"]):
        return "test_bounds_exceeded"
    return None


def parse_result(text):
    """result.json written by tests/load/cdr.js -> the ops.test_runs.summary object. Missing or non-numeric core fields are an error."""
    try:
        r = json.loads(text)
        p95, err, reqs = r["p95_ms"], r["error_rate"], r["requests"]
    except (ValueError, KeyError, TypeError):
        raise LoadTestError("result_unparsable")
    if not all(isinstance(x, (int, float)) for x in (p95, err, reqs)):
        raise LoadTestError("result_unparsable")
    return {
        "p95_ms": round(p95, 1), "error_rate": round(err, 6), "requests": int(reqs),
        "achieved_rps": round(r.get("achieved_rps") or 0, 2), "dropped_iterations": int(r.get("dropped_iterations") or 0),
    }


class K6Host:
    def __init__(self, host, user, key, known_hosts=None, timeout=20, run=None):
        if not HOST_RE.fullmatch(host or "") or not USER_RE.fullmatch(user or ""):
            raise LoadTestError("testing_ssh_config_invalid")
        self.host, self.user, self.key, self.known_hosts, self.timeout = host, user, key, known_hosts, timeout
        self._run = run or self._exec

    @classmethod
    def from_config(cls, config, run=None):
        c = (config or {}).get("testing_ssh")
        if not isinstance(c, dict):
            raise LoadTestError("testing_ssh_config_missing")
        return cls(c.get("host"), c.get("user"), c.get("key"), c.get("known_hosts"), run=run)

    def _opts(self):
        o = ["-i", self.key, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes", "-o", f"ConnectTimeout={self.timeout}"]
        return o + (["-o", f"UserKnownHostsFile={self.known_hosts}"] if self.known_hosts else [])

    def _exec(self, argv):
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout * 2, check=False)
            return p.returncode, p.stdout
        except (OSError, subprocess.TimeoutExpired):
            return 255, ""

    def _call(self, *words):
        code, out = self._run(["ssh", *self._opts(), f"{self.user}@{self.host}", " ".join(words)])
        if code == 64:
            raise LoadTestError("k6ctl_refused")
        if code != 0:
            raise LoadTestError("testing_unreachable")
        return out

    def start(self, run_id, scenario, rate, duration, work_ms):
        if not RUN_ID_RE.fullmatch(run_id) or scenario not in SCENARIOS or not all(isinstance(n, int) and 0 < n < 10000 for n in (rate, duration, work_ms)):
            raise LoadTestError("bad_argument")
        self._call("start", run_id, scenario, str(rate), str(duration), str(work_ms))

    def stop(self):
        self._call("stop")

    def status(self):
        try:
            return json.loads(self._call("status"))
        except ValueError:
            raise LoadTestError("status_unparsable")

    def result(self, run_id):
        if not RUN_ID_RE.fullmatch(run_id):
            raise LoadTestError("bad_argument")
        return parse_result(self._call("result", run_id))
