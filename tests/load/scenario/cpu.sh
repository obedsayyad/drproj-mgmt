#!/bin/bash
# Fleet CPU (serving members), the CdrCpuHigh/Low expression, plus firing CDR alerts
Q='100 * (1 - sum(rate(node_cpu_seconds_total{role=~"app|cloud",mode="idle"}[30s]) and on(instance) (cdr_member_serving == 1)) / count(rate(node_cpu_seconds_total{role=~"app|cloud",mode="idle"}[30s]) and on(instance) (cdr_member_serving == 1)))'
V=$(docker exec drproj-mgmt-prometheus-1 wget -qO- "http://localhost:9090/api/v1/query?query=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "$Q")" | python3 -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(round(float(r[0]["value"][1]),1) if r else "n/a")')
A=$(docker exec drproj-mgmt-prometheus-1 wget -qO- 'http://localhost:9090/api/v1/alerts' | python3 -c 'import sys,json;print(",".join(a["labels"]["alertname"]+":"+a["state"] for a in json.load(sys.stdin)["data"]["alerts"]))')
echo "  cpu_fleet=$V alerts=[$A]"
