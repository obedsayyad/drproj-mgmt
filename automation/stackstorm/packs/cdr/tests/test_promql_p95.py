"""The failback p95 gate failed twice at ramp_10 (ops 73306ee5, 10564920) on the failback's own direct /health/ready probes: that route does a
database round trip (0.1-1.3 s) and, with no user traffic, was the whole sample on the primary, against a ~5 ms idle baseline."""
import re

import promql


def _selector(expr):
    m = re.search(r"relief_http_request_duration_seconds_bucket(\{[^}]*\})?\[", expr)
    assert m, expr
    return m.group(1) or ""


def test_gate_and_baseline_p95_exclude_the_ready_probe_route():
    for expr in (promql.q_p95("60s"), promql.q_baseline()):
        assert 'route!="/health/ready"' in _selector(expr)


def test_gate_p95_keeps_the_live_check_route_so_an_idle_primary_still_has_samples():
    assert "/health/live" not in promql.q_p95("60s")
    assert re.fullmatch(r'histogram_quantile\(0\.95, sum by \(le\) \(rate\(relief_http_request_duration_seconds_bucket\{route!="/health/ready"\}\[60s\]\)\)\)', promql.q_p95("60s"))


def test_judge_still_fails_closed_without_samples_or_baseline():
    fb = {"gate_max_5xx_ratio": 0.01, "gate_max_p95_ratio_to_baseline": 1.5, "gate_max_primary_cpu_percent": 50}
    vals = {"5xx": 0.0, "p95": None, "cpu": 1.0, "fresh": 10}
    assert not promql.judge(vals, 0.005, fb, 5)["p95"]["ok"]
    assert not promql.judge({**vals, "p95": 0.005}, None, fb, 5)["p95"]["ok"]
    assert promql.judge({**vals, "p95": 0.006}, 0.005, fb, 5)["p95"]["ok"]


def test_keepalive_rule_is_a_wall_clock_cron_not_an_interval_that_every_deploy_resets():
    import yaml
    rule = yaml.safe_load(open(__file__.replace("test_promql_p95.py", "../rules/keepalive_every_6h.yaml")))
    assert rule["trigger"]["type"] == "core.st2.CronTimer" and rule["enabled"] is True
    assert rule["trigger"]["parameters"]["hour"] == "*/6" and rule["trigger"]["parameters"]["timezone"] == "UTC"


def test_probe_failure_names_the_path_and_error_class_not_the_message(monkeypatch):
    import failback_lib
    import requests

    def fake_get(url, timeout, verify):
        if url.endswith("/health/ready"):
            raise requests.ConnectTimeout("https://secret-host:8443 timed out")
        return type("R", (), {"status_code": 200})()

    monkeypatch.setattr(failback_lib.requests, "get", fake_get)
    res = failback_lib.probe("10.0.0.1", None)
    assert tuple(res) == (True, False)
    assert len(res.detail) == 1 and res.detail[0]["path"] == "/health/ready" and res.detail[0]["error"] == "ConnectTimeout"
    assert "secret-host" not in str(res.detail)
