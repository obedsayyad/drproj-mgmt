#!/bin/sh
# Fake ansible-playbook for the ansible_deploy tests. Behaviour is read from "mode" next to this script (words, one per line):
#   fail | no_stats | wrong_identity | sleep=N (seconds) | digest=sha256:...
# argv is appended to "argv" and the secret-bearing env names that reached the process to "env", both next to this script.
here=$(dirname "$0")
echo "$*" >> "$here/argv"
env | sed -n 's/^\(DR_AUTOMATION_URL\|NODE_EXPORTER_BASIC_AUTH_PASSWORD\|SUPABASE_APP_URL\|ANSIBLE_[A-Z_]*\|CLOUD_VM_SSH_KEY_FILE\|DIGITALOCEAN_TOKEN\|LINODE_TOKEN\)=.*/\1/p' | sort > "$here/env"
mode() { grep -qx "$1" "$here/mode" 2>/dev/null; }
value() { sed -n "s/^$1=//p" "$here/mode" 2>/dev/null | head -n 1; }
limit=
prev=
for a in "$@"; do [ "$prev" = "--limit" ] && limit=$a; prev=$a; done
s=$(value sleep); [ -z "$s" ] || sleep "$s"
mode fail && { echo '{"plays": [], "stats": {}, "custom_stats": {}}'; exit 2; }
digest=$(value digest); digest=${digest:-sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}
ident=$limit; mode wrong_identity && ident=some-other-instance
if mode no_stats; then
  echo '{"plays": [], "stats": {}, "custom_stats": {}, "global_custom_stats": {}}'
else
  printf '{"plays": [], "stats": {}, "custom_stats": {"%s": {"ready": true, "instance_id": "%s", "digest": "%s"}}, "global_custom_stats": {}}\n' "$limit" "$ident" "$digest"
fi
exit 0
