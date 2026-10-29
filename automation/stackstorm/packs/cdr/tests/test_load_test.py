import json

import pytest

import load_test as action_mod
from k6_host import LoadTestError, K6Host, check_bounds, parse_result

DEMO = {"max_rate_rps": 200, "max_duration_seconds": 600, "max_work_ms": 250, "default_work_ms": 100}
GOOD = json.dumps({"p95_ms": 41.234, "error_rate": 0, "requests": 600, "achieved_rps": 9.99, "dropped_iterations": 0})


@pytest.mark.parametrize("args,code", [
    (("smoke", 10, 60, 100), None), (("smoke", 200, 600, 250), None), (("smoke", 201, 60, 100), "test_bounds_exceeded"),
    (("smoke", 10, 601, 100), "test_bounds_exceeded"), (("smoke", 10, 60, 251), "test_bounds_exceeded"), (("rm", 10, 60, 100), "bad_scenario"),
    (("smoke", 0, 60, 100), "test_bounds_exceeded"),
])
def test_bounds(args, code):
    assert check_bounds(DEMO, *args) == code


def test_parse_result_keeps_the_contract_fields():
    s = parse_result(GOOD)
    assert (s["p95_ms"], s["error_rate"], s["requests"]) == (41.2, 0, 600)


@pytest.mark.parametrize("text", ["", "{}", '{"p95_ms": null, "error_rate": 0, "requests": 1}', "not json"])
def test_parse_result_rejects_incomplete(text):
    with pytest.raises(LoadTestError, match="result_unparsable"):
        parse_result(text)


def make(out="", code=0):
    calls = []

    def run(argv):
        calls.append(argv)
        return code, out
    return K6Host("203.0.113.9", "root", "/k", "/kh", run=run), calls


def test_start_is_a_fixed_argv_with_one_remote_command():
    t, calls = make()
    t.start("abc-123", "smoke", 10, 60, 100)
    argv = calls[0]
    assert argv[0] == "ssh" and argv[-2] == "root@203.0.113.9" and argv[-1] == "start abc-123 smoke 10 60 100"
    assert "StrictHostKeyChecking=yes" in argv and "BatchMode=yes" in argv


@pytest.mark.parametrize("run_id,scenario", [("a;rm -rf /", "smoke"), ("ok", "drop"), ("x" * 41, "smoke"), ("a b", "smoke")])
def test_start_refuses_shell_text(run_id, scenario):
    t, calls = make()
    with pytest.raises(LoadTestError, match="bad_argument"):
        t.start(run_id, scenario, 10, 60, 100)
    assert not calls


def test_refused_and_unreachable_are_distinct_codes():
    with pytest.raises(LoadTestError, match="k6ctl_refused"):
        make(code=64)[0].stop()
    with pytest.raises(LoadTestError, match="testing_unreachable"):
        make(code=255)[0].stop()


def test_bad_host_config_refused():
    with pytest.raises(LoadTestError, match="testing_ssh_config_invalid"):
        K6Host("host;x", "root", "/k")
    with pytest.raises(LoadTestError, match="testing_ssh_config_missing"):
        K6Host.from_config({})


class Fake:
    def __init__(self, statuses, run_status="running", result=GOOD):
        self.statuses, self.run_status, self.result_text, self.stopped, self.projected = list(statuses), run_status, result, False, []

    def status(self):
        return self.statuses.pop(0)

    def stop(self):
        self.stopped = True

    def result(self, run_id):
        return parse_result(self.result_text)


def wait(monkeypatch, fake, duration=1):
    a = action_mod.LoadTest(config={})
    monkeypatch.setattr(action_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(a, "_project", lambda rid, st, summary=None: fake.projected.append((st, summary)) or (True, "x"))
    monkeypatch.setattr(a, "_status_of", lambda rid: fake.run_status)
    return a._wait(fake, "rid", duration)


def test_wait_success_projects_summary(monkeypatch):
    f = Fake([{"state": "running"}, {"state": "exited", "exit_code": 0}])
    ok, res = wait(monkeypatch, f)
    assert ok and res["state"] == "succeeded" and f.projected[-1][0] == "succeeded" and f.projected[-1][1]["requests"] == 600


def test_wait_cancelled_when_stop_was_requested(monkeypatch):
    f = Fake([{"state": "exited", "exit_code": 137}], run_status="cancelling")
    ok, res = wait(monkeypatch, f)
    assert res["state"] == "cancelled" and f.projected == [("cancelled", None)]


def test_wait_crash_is_failed_not_succeeded(monkeypatch):
    f = Fake([{"state": "exited", "exit_code": 1}])
    ok, res = wait(monkeypatch, f)
    assert res["state"] == "failed" and f.projected[-1][0] == "failed"


def test_wait_deadline_kills_k6(monkeypatch):
    f = Fake([{"state": "running"}] * 5)
    ticks = iter(range(0, 1000, 500))
    monkeypatch.setattr(action_mod.time, "monotonic", lambda: next(ticks))
    ok, res = wait(monkeypatch, f)
    assert f.stopped and res["error"] == "timeout"


def test_stop_marks_the_active_run_cancelling_then_kills_k6(monkeypatch):
    """The stop task reads the one non-terminal run (cdr_db.call takes a params tuple), projects cancelling, then sends the stop verb."""
    sent, projected = [], []
    monkeypatch.setattr(action_mod.cdr_db, "call", lambda cfg, sql, params: (True, [("run-1", "running")]) if "FROM ops.test_runs" in sql else (False, "unexpected"))
    monkeypatch.setattr(K6Host, "stop", lambda self: sent.append("stop"))
    a = action_mod.LoadTest(config={"testing_ssh": {"host": "203.0.113.9", "user": "root", "key": "/k"}})
    monkeypatch.setattr(a, "_project", lambda rid, st, summary=None: projected.append((rid, st)) or (True, "x"))
    ok, res = a.run("stop", "op-1")
    assert ok and res == {"run_id": "run-1", "stopped": True} and projected == [("run-1", "cancelling")] and sent == ["stop"]


def test_stop_without_an_active_run_fails_not_running(monkeypatch):
    monkeypatch.setattr(action_mod.cdr_db, "call", lambda cfg, sql, params: (True, []))
    a = action_mod.LoadTest(config={"testing_ssh": {"host": "203.0.113.9", "user": "root", "key": "/k"}})
    assert a.run("stop", "op-1") == (False, "test_not_running")
