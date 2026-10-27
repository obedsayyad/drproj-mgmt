#!/bin/sh
# Fails unless secret material is owner-only (docs/08 "Operational ports", docs/11 hardening):
#   - every .env / secret file is mode 0600 and owned by the current user;
#   - every directory that holds them is mode 0700;
#   - backup and Terraform-state directories are 0700 and everything under them is 0600/0700.
# Prints paths and modes only, never file contents.
#
# Usage: check-perms.sh [PATH...]
#   FILE  -> must be 0600, and its parent directory must be 0700
#   DIR   -> must be 0700; files below must be 0600 and subdirectories 0700
#            (README.md, .gitignore, *.example and .gitkeep are documentation, not secrets, and are skipped)
#   no args -> deploy/compose/*/.env, deploy/compose/*/secrets, $BACKUP_DIR and $TFSTATE_DIR (those that are set/exist)
set -u

if stat -c '%a' / >/dev/null 2>&1; then
  mode_of() { stat -c '%a' "$1"; }
  owner_of() { stat -c '%u' "$1"; }
else
  mode_of() { stat -f '%Lp' "$1"; }
  owner_of() { stat -f '%u' "$1"; }
fi

me=$(id -u)
fail=0
bad() { printf 'FAIL %s\n' "$1"; fail=1; }
ok() { printf 'ok   %s\n' "$1"; }

check() { # path want_mode
  p=$1; want=$2
  m=$(mode_of "$p"); o=$(owner_of "$p")
  if [ "$m" != "$want" ]; then bad "$p mode $m (want $want)"
  elif [ "$o" != "$me" ]; then bad "$p owner uid $o (want $me)"
  else ok "$p $m"; fi
}

is_doc() { case "$(basename "$1")" in README.md|.gitignore|.gitkeep|*.example) return 0 ;; esac; return 1; }

check_dir() {
  d=$1
  check "$d" 700
  find "$d" -mindepth 1 | while IFS= read -r f; do
    is_doc "$f" && continue
    if [ -d "$f" ]; then check "$f" 700; else check "$f" 600; fi
  done
}

check_target() {
  t=$1
  if [ ! -e "$t" ]; then bad "$t does not exist"; return; fi
  if [ -d "$t" ]; then check_dir "$t"; else check "$t" 600; check "$(dirname "$t")" 700; fi
}

if [ "$#" -eq 0 ]; then
  root=$(cd "$(dirname "$0")/.." && pwd)
  set --
  for f in "$root"/deploy/compose/*/.env "$root"/deploy/compose/*/secrets; do [ -e "$f" ] && set -- "$@" "$f"; done
  [ -n "${BACKUP_DIR:-}" ] && set -- "$@" "$BACKUP_DIR"
  [ -n "${TFSTATE_DIR:-}" ] && set -- "$@" "$TFSTATE_DIR"
  if [ "$#" -eq 0 ]; then echo "nothing to check"; exit 0; fi
fi

# Run in the current shell (not a pipeline subshell) so `fail` propagates; collect output of the loop helpers.
out=$(for t in "$@"; do check_target "$t"; done)
printf '%s\n' "$out"
printf '%s\n' "$out" | grep -q '^FAIL' && fail=1
exit "$fail"
