#!/bin/bash
# HAProxy be_app server state from the LB stats CSV (8404, TLS verified with the project CA); MAINT-only servers are counted, not listed.
curl -s -m 10 --cacert /etc/drproj/ca/ca-bundle.crt -u "prometheus:$(cat /etc/drproj/mgmt/secrets/haproxy_stats_password)" "https://103.141.79.172:8404/stats;csv" | awk -F, '$1=="be_app" && $2!="BACKEND" { if ($18 ~ /MAINT/) m++; else print "  hap", $2, $18, "w"$19, ($21==1?"backup":"active"), "scur="$5, "stot="$8, "2xx="$41, "5xx="$44, "chk="$37 } END { print "  hap (maint:", m+0, ")" }'
