#!/bin/sh
# Fake terraform for the real-adapter tests. Behaviour is read from $TF_STATE_DIR/.fake/mode (words, one per line):
#   fail_init | fail_before_write | fail_after_write | fail_output | sleep=N (seconds, before the state write) | rid=ID
# Every apply's argv is appended to $TF_STATE_DIR/.fake/apply_args. State is written where tf-slot.sh pointed the backend.
ctl=$TF_STATE_DIR/.fake
mode() { grep -qx "$1" "$ctl/mode" 2>/dev/null; }
value() { sed -n "s/^$1=//p" "$ctl/mode" 2>/dev/null | head -n 1; }
rid=$(value rid); rid=${rid:-424242}
cmd=$1
case $cmd in
  init)
    for a in "$@"; do case $a in -backend-config=path=*) printf '%s' "${a#-backend-config=path=}" > "$ctl/state_path" ;; esac; done
    mode fail_init && exit 1
    exit 0 ;;
  apply)
    echo "$*" >> "$ctl/apply_args"
    exists=true
    for a in "$@"; do [ "$a" = "exists=false" ] && exists=false; done
    s=$(value sleep); [ -z "$s" ] || sleep "$s"
    mode fail_before_write && exit 1
    state=$(cat "$ctl/state_path")
    if [ "$exists" = true ]; then
      printf '{"version":4,"resources":[{"mode":"managed","type":"fake_instance","instances":[{"attributes":{"id":"%s"}}]}]}' "$rid" > "$state"
    else
      printf '{"version":4,"resources":[]}' > "$state"
    fi
    mode fail_after_write && exit 1
    exit 0 ;;
  output)
    mode fail_output && exit 1
    state=$(cat "$ctl/state_path" 2>/dev/null)
    if [ -n "$state" ] && grep -q "\"id\":\"$rid\"" "$state" 2>/dev/null; then
      printf '{"resource_id":{"value":"%s"},"public_ip":{"value":"203.0.113.9"},"provider_state":{"value":"active"}}' "$rid"
    else
      printf '{"resource_id":{"value":null},"public_ip":{"value":null},"provider_state":{"value":null}}'
    fi
    exit 0 ;;
esac
exit 0
