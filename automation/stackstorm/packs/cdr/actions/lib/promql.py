"""PromQL for the failback gates (docs/04) and the pre-failure baseline. Instant queries only; no data is a failed gate (fail closed).

Windows: rates use [60s] and are evaluated when the observation window ends, so every sample behind a gate value was taken after
the step started (observation >= 60 s). `fresh` additionally requires the primary's newest response sample to be newer than the step start.
"""
import json
import urllib.parse
import urllib.request


class PromError(Exception):
    pass


P95 = 'histogram_quantile(0.95, sum by (le) (rate(relief_http_request_duration_seconds_bucket[{w}])))'
RESP = 'haproxy_server_http_responses_total{{proxy="be_app",server="primary"{extra}}}'


def q_5xx_ratio(w="60s"):
    errors = RESP.format(extra=',code="5xx"')
    total = RESP.format(extra="")
    return f"sum(rate({errors}[{w}])) / sum(rate({total}[{w}]))"


def q_p95(w="60s"):
    return P95.format(w=w)


def q_cpu(primary_ip, w="60s"):
    return f'100 * (1 - avg(rate(node_cpu_seconds_total{{role="app",mode="idle",instance=~"{primary_ip}:.*"}}[{w}])))'


def q_fleet_cpu(w="30s"):
    """The CdrCpuHigh/CdrCpuLow expression (monitoring/rules/cdr.yml): CPU of the serving members, vCPU-weighted."""
    sel = f'rate(node_cpu_seconds_total{{role=~"app|cloud",mode="idle"}}[{w}]) and on(instance) (cdr_member_serving == 1)'
    return f"100 * (1 - sum({sel}) / count({sel}))"


def q_fresh():
    return f'max(timestamp({RESP.format(extra="")}))'


def q_baseline():
    """Median of the app p95 (1 m windows) over the 10 minutes ending 1 m ago (before the detection lag); evaluated when the primary failure is recorded."""
    return f'quantile_over_time(0.5, ({P95.format(w="1m")})[10m:1m] offset 1m)'


def instant(url, expr, timeout=5):
    """-> float or None (empty result). Raises PromError when Prometheus cannot be read."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": expr}), timeout=timeout) as r:  # noqa: S310 - operator-configured URL
            body = json.load(r)
        if body.get("status") != "success":
            raise PromError("prometheus_error")
        res = body["data"]["result"]
        if not res:
            return None
        v = float(res[0]["value"][1])
        return None if v != v else v  # NaN is no data
    except PromError:
        raise
    except Exception:  # noqa: BLE001 - short code only
        raise PromError("prometheus_unreachable")


def judge(values, baseline, fb, step_started):
    """values: {5xx, p95, cpu, fresh}; fb: policy failback section. -> {gate: {"ok": bool, "value": ...}}; None values fail."""
    def gate(name, ok, value):
        return {"ok": bool(ok), "value": value}

    v5, p95, cpu, fresh = (values.get(k) for k in ("5xx", "p95", "cpu", "fresh"))
    return {
        "samples_after_start": gate("fresh", fresh is not None and fresh > step_started, fresh),
        "5xx": gate("5xx", v5 is not None and v5 < fb["gate_max_5xx_ratio"], v5),
        "p95": gate("p95", p95 is not None and baseline is not None and p95 < fb["gate_max_p95_ratio_to_baseline"] * baseline, p95),
        "cpu": gate("cpu", cpu is not None and cpu < fb["gate_max_primary_cpu_percent"], cpu),
    }
