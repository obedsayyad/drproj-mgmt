#!/bin/bash
# usage: watch.sh LABEL SECONDS  -> appends a compact state sample every 20 s
L=$1; N=$2; end=$(( $(date +%s) + N ))
while [ $(date +%s) -lt $end ]; do
  { echo "-- $L $(date -u +%T)"; /opt/drproj/p63/state.sh 5; } >> /opt/drproj/p63/watch-$L.log 2>&1
  sleep 20
done
