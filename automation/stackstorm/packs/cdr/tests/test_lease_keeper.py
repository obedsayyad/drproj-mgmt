"""lease_keeper: 2 consecutive failed renewals stop the worker; a definitive lease_lost stops at once."""
import time

import pytest

from lease_keeper import LeaseKeeper, LeaseLost, fenced


def keeper(results, **kw):
    it = iter(results)
    return LeaseKeeper({}, "op", 1, interval=kw.pop("interval", 0.01), renew=lambda: next(it), **kw)


def test_one_failure_is_tolerated_and_a_success_resets_the_count():
    k = keeper([(False, "connect_or_network"), (True, None), (False, "connect_or_network")])
    k.renew_once(); k.renew_once(); k.renew_once()
    assert not k.stopped.is_set() and k.failures == 1
    k.check()


def test_two_consecutive_failures_set_the_stop_flag_and_check_raises():
    k = keeper([(False, "connect_or_network"), (False, "connect_or_network")])
    k.renew_once()
    assert not k.stopped.is_set()
    k.renew_once()
    assert k.stopped.is_set()
    with pytest.raises(LeaseLost):
        k.check()


def test_lease_lost_from_the_database_stops_immediately():
    k = keeper([(False, "lease_lost")])
    k.renew_once()
    assert k.stopped.is_set()


def test_background_thread_renews_until_it_fails_twice():
    calls = []

    def renew():
        calls.append(time.monotonic())
        return (True, None) if len(calls) < 4 else (False, "connect_or_network")

    k = LeaseKeeper({}, "op", 1, interval=0.02, renew=renew)
    with k:
        assert k.stopped.wait(2)
    assert len(calls) == 5  # three renewals succeed, then exactly two fail


def test_exit_stops_the_thread_even_while_healthy():
    k = LeaseKeeper({}, "op", 1, interval=0.01, renew=lambda: (True, None))
    with k:
        time.sleep(0.05)
    assert not k._thread.is_alive() and not k.stopped.is_set()


def test_fenced_runs_nothing_when_the_keeper_has_stopped(monkeypatch):
    k = keeper([(False, "lease_lost")])
    k.renew_once()
    ran = []
    with pytest.raises(LeaseLost):
        fenced({}, "op", 1, lambda: ran.append(1), k)
    assert ran == []
