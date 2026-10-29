#!/bin/bash
# inject.sh : when HAProxy shows the primary at the 50% step (weight 256 and warm_act still serving, i.e. not the final normal mode),
# stop relief on the primary for 25 s (a regression the direct probes must see), then start it again. Fires once.
while true; do
  st=$(curl -s -m 10 --cacert /etc/drproj/ca/ca-bundle.crt -u "prometheus:$(cat /etc/drproj/mgmt/secrets/haproxy_stats_password)" "https://103.141.79.172:8404/stats;csv" | awk -F, '$1=="be_app" && ($2=="primary" || $2=="warm_act") {print $2"="$18"/"$19}' | tr '\n' ' ')
  case "$st" in
    *"primary=UP/256"*"warm_act=UP"*|*"warm_act=UP"*"primary=UP/256"*)
      echo "$(date -u +%T) 50% step seen ($st): stopping relief on primary for 25 s" >> /opt/drproj/p63/inject.log
      ssh -i ~/.ssh/drproj root@103.141.79.171 "docker stop drproj-app-relief-1; sleep 25; docker start drproj-app-relief-1" >> /opt/drproj/p63/inject.log 2>&1
      echo "$(date -u +%T) injection done" >> /opt/drproj/p63/inject.log
      exit 0;;
  esac
  sleep 2
done
