"""Prometheus reports lastScrape with nanoseconds; the freshness gate must read them (the first real scale-in died on this)."""
from datetime import datetime, timedelta, timezone

import pytest

import metrics


@pytest.mark.parametrize("text", [
    "2026-10-29T15:31:20.790539617Z",
    "2026-10-29T15:31:20.790539617+00:00",
    "2026-10-29T15:31:20.79Z",
    "2026-10-29T15:31:20Z",
    "2026-10-29T17:31:20.790539617+02:00",
    "2026-10-29T15:31:20.790539+0000",
])
def test_scrape_times_with_nanoseconds_and_any_offset_parse_to_the_same_instant(text):
    got = metrics.parse_scrape_time(text)
    assert abs((got - datetime(2026, 10, 29, 15, 31, 20, tzinfo=timezone.utc)).total_seconds()) < 1
    assert got.utcoffset() is not None


def test_an_unreadable_scrape_time_is_not_fresh(monkeypatch):
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    target = lambda ts: {"labels": {"job": "relief"}, "health": "up", "lastScrape": ts}

    class Resp:
        def __init__(self, ts):
            self.ts = ts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            import json
            return json.dumps({"data": {"activeTargets": [target(self.ts)]}}).encode()

    for ts, expected in ((now, True), ("not a time", False), ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), False)):
        monkeypatch.setattr(metrics.urllib.request, "urlopen", lambda *a, _ts=ts, **k: Resp(_ts))
        assert metrics.prometheus_fresh("http://p", ["relief"], 120) is expected
