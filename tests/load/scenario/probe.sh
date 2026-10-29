#!/bin/bash
# probe.sh LABEL SECONDS : one GET /health/live and one GET / through the LB every 0.5 s; log "epoch code instance"
L=$1; N=$2; end=$(( $(date +%s) + N ))
while [ $(date +%s) -lt $end ]; do
  r=$(curl -s -o /dev/null -m 5 -D - -w '%{http_code}' https://app.cproj.obed.one/health/live 2>/dev/null | tr -d '\r' | awk '/^x-instance-id/{i=$2} END{printf "%s", i}')
  c=$(curl -s -o /dev/null -m 5 -w '%{http_code}' https://app.cproj.obed.one/health/live)
  echo "$(date +%s) $c $r" >> /tmp/probe-$L.log
  sleep 0.5
done
