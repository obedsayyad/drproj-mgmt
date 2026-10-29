#!/bin/bash
# usage: api.sh METHOD PATH [JSON]   (operator auth from the host secret; output = HTTP code + body)
PW=$(cat /etc/drproj/secrets/ops-operator.pw)
M=$1; P=$2; B=${3:-}
K=$(head -c 12 /dev/urandom | base64 | tr -d '+/=' | head -c 16)
if [ -n "$B" ]; then
  curl -s -m 30 -u "operator:$PW" -X "$M" -H "Idempotency-Key: p63-$K" -H 'Content-Type: application/json' -d "$B" -w '\nHTTP %{http_code}\n' "https://ops.cproj.obed.one$P"
else
  curl -s -m 30 -u "operator:$PW" -X "$M" -H "Idempotency-Key: p63-$K" -w '\nHTTP %{http_code}\n' "https://ops.cproj.obed.one$P"
fi
