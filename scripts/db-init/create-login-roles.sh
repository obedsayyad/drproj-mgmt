#!/bin/bash
# Creates LOGIN roles bound to dr_* group roles; writes per-role env files (0600). Idempotent: existing env file => keep password.
set -euo pipefail
umask 077
S=/etc/drproj/secrets
REF=dhppvnbwxkfrktjkrsqp
HOST=aws-0-ap-southeast-1.pooler.supabase.com
. $S/db-migration.env
psqlm() { docker run --rm -i postgres:17-alpine psql "$SUPABASE_MIGRATION_URL" -v ON_ERROR_STOP=1 -qAt "$@"; }
declare -A GROUP=( [app_runtime]=dr_app_runtime [ops]=dr_ops [backup]=dr_backup [restore]=dr_restore [automation]=dr_automation )
declare -A VAR=( [app_runtime]=SUPABASE_APP_URL [ops]=SUPABASE_OPS_URL [backup]=SUPABASE_BACKUP_URL [restore]=SUPABASE_RESTORE_URL [automation]=DR_AUTOMATION_URL )
for r in app_runtime ops backup restore automation; do
  f=$S/db-$r.env
  if [ -f $f ]; then pw=$(sed -n "s#^${VAR[$r]}=postgresql://[^:]*:\([^@]*\)@.*#\1#p" $f); else pw=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32); fi
  [ ${#pw} -eq 32 ] || { echo "bad pw len for $r"; exit 1; }
  psqlm <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='$r') THEN CREATE ROLE $r LOGIN PASSWORD '$pw'; ELSE ALTER ROLE $r LOGIN PASSWORD '$pw'; END IF;
END \$\$;
GRANT ${GROUP[$r]} TO $r;
SQL
  printf '%s=postgresql://%s.%s:%s@%s:5432/postgres?sslmode=require\n' "${VAR[$r]}" "$r" "$REF" "$pw" "$HOST" > $f
  chmod 600 $f
  echo "ok $r -> ${GROUP[$r]} ($f)"
done
