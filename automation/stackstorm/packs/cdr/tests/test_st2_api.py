"""St2Api.run retries a dispatch 3x with backoff on 5xx/timeout, and never starts a second execution after a timeout that landed."""
import pytest

import st2_api
from st2_api import St2Api, St2Unavailable


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(st2_api.time, "sleep", lambda _s: None)


def api_with(monkeypatch, script, live=()):
    calls = []

    def req(self, method, path, body=None):
        if method == "GET":
            return [{"id": i, "status": "running", "parameters": {"operation_id": "op1"}} for i in live]
        calls.append(body)
        r = script[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(St2Api, "_req", req)
    return St2Api(base="http://st2"), calls


def test_dispatch_retries_5xx_then_succeeds(monkeypatch):
    api, calls = api_with(monkeypatch, [St2Unavailable("http_503"), St2Unavailable("http_502"), {"id": "e1"}])
    assert api.run("cdr.scale_in", {"operation_id": "op1"}) == "e1" and len(calls) == 3


def test_dispatch_gives_up_after_three_attempts(monkeypatch):
    api, calls = api_with(monkeypatch, [St2Unavailable("http_500")] * 3)
    with pytest.raises(St2Unavailable):
        api.run("cdr.scale_in", {"operation_id": "op1"})
    assert len(calls) == 3


def test_dispatch_does_not_retry_a_4xx(monkeypatch):
    api, calls = api_with(monkeypatch, [St2Unavailable("http_400")])
    with pytest.raises(St2Unavailable):
        api.run("cdr.scale_in", {"operation_id": "op1"})
    assert len(calls) == 1


def test_a_timeout_that_actually_landed_returns_the_live_execution_instead_of_a_duplicate(monkeypatch):
    api, calls = api_with(monkeypatch, [St2Unavailable("TimeoutError")], live=["landed1"])
    assert api.run("cdr.scale_in", {"operation_id": "op1"}) == "landed1" and len(calls) == 1
