"""Load-test steps for start_load_test / stop_load_test (P6.3). k6 runs on the testing host through lib/k6_host.py (forced-command SSH);
ops.test_runs is projected through ops.update_test_run. No capacity lease: a load test changes no capacity state."""
import time

import cdr_db
from cdr_common import guarded, policy_section
from k6_host import LoadTestError, K6Host, check_bounds
from st2common.runners.base_action import Action

POLL_SECONDS = 5
FINISH_MARGIN_S = 90
RUN_SQL = "SELECT id::text, scenario, rate_rps, duration_seconds, status FROM ops.test_runs WHERE operation_id = %s::uuid"
ACTIVE_SQL = "SELECT id::text, status FROM ops.test_runs WHERE status NOT IN ('succeeded', 'failed', 'cancelled') ORDER BY created_at DESC"


class LoadTest(Action):
    def _project(self, run_id, status, summary=None):
        import json
        return cdr_db.call(self.config, "SELECT ops.update_test_run(%s::uuid, %s, %s, %s::jsonb)",
                           (run_id, status, f"testing:/opt/drproj/k6/results/{run_id}" if status != "cancelling" else None,
                            None if summary is None else json.dumps(summary)))

    def _status_of(self, run_id):
        ok, rows = cdr_db.call(self.config, "SELECT status FROM ops.test_runs WHERE id = %s::uuid", (run_id,))
        return rows[0][0] if ok and rows else None

    @guarded
    def run(self, task, operation_id, run_id=None, work_ms=None, duration_seconds=None):
        try:
            return self._run(task, operation_id, run_id, work_ms, duration_seconds)
        except LoadTestError as exc:
            return False, str(exc)

    def _run(self, task, operation_id, run_id, work_ms, duration_seconds):
        if task == "validate":
            ok, rows = cdr_db.call(self.config, RUN_SQL, (operation_id,))
            if not ok:
                return False, rows
            if len(rows) != 1:
                return False, "test_run_not_found"
            ok, demo = policy_section(self.config, operation_id, "demo")
            if not ok:
                return False, demo
            rid, scenario, rate, duration, _ = rows[0]
            work = demo["default_work_ms"]
            err = check_bounds(demo, scenario, rate, duration, work)
            if err:
                return False, err
            return True, {"run_id": rid, "scenario": scenario, "rate_rps": rate, "duration_seconds": duration, "work_ms": work}
        t = K6Host.from_config(self.config)
        if task == "start":
            run = self._get(operation_id)
            t.start(run["run_id"], run["scenario"], run["rate_rps"], run["duration_seconds"], work_ms)
            ok, code = self._project(run["run_id"], "running")
            return (True, {"run_id": run["run_id"]}) if ok else (False, code)
        if task == "wait":
            return self._wait(t, run_id, duration_seconds)
        if task == "stop":
            ok, rows = cdr_db.call(self.config, ACTIVE_SQL)
            if not ok:
                return False, rows
            if not rows:
                return False, "test_not_running"
            rid = rows[0][0]
            self._project(rid, "cancelling")
            t.stop()
            return True, {"run_id": rid, "stopped": True}
        return False, "bad_argument"

    def _get(self, operation_id):
        ok, rows = cdr_db.call(self.config, RUN_SQL, (operation_id,))
        r = rows[0]
        return {"run_id": r[0], "scenario": r[1], "rate_rps": r[2], "duration_seconds": r[3]}

    def _wait(self, t, run_id, duration_seconds):
        """Poll k6ctl status until the container ends. cancelling in ops.test_runs (stop_load_test) means the exit was requested."""
        deadline = time.monotonic() + duration_seconds + FINISH_MARGIN_S
        while True:
            st = t.status()
            if st.get("state") != "running":
                break
            if time.monotonic() > deadline:
                t.stop()
                self._project(run_id, "failed", {"error": "timeout"})
                return True, {"state": "failed", "error": "timeout", "run_id": run_id}
            time.sleep(POLL_SECONDS)
        if self._status_of(run_id) == "cancelling":
            self._project(run_id, "cancelled")
            return True, {"state": "cancelled", "run_id": run_id}
        if st.get("state") == "none" or st.get("exit_code") not in (0, 99):
            # 99 = thresholds crossed (none are configured); anything else is a crash or a kill from outside.
            self._project(run_id, "failed", {"error": f"k6_exit_{st.get('exit_code')}"})
            return True, {"state": "failed", "error": f"k6_exit_{st.get('exit_code')}", "run_id": run_id}
        summary = t.result(run_id)
        self._project(run_id, "succeeded", summary)
        return True, {"state": "succeeded", "summary": summary, "run_id": run_id}
