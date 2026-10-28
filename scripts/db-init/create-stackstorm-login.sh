#!/bin/bash
# Creates the `stackstorm` LOGIN role used by the cdr pack (supabase_ops_url): a member of dr_ops AND dr_automation, as
# automation/stackstorm/packs/cdr/config.schema.yaml requires. Writes /etc/drproj/secrets/db-stackstorm.env (0600).
# Idempotent: an existing env file keeps its password. Run on management with the migration login (docs/15 §8).
set -euo pipefail
umask 077
S=/etc/drproj/secrets
REF=dhppvnbwxkfrktjkrsqp
HOST=aws-0-ap-southeast-1.pooler.supabase.com
r=stackstorm
f=$S/db-$r.env
. $S/db-migration.env
psqlm() { docker run --rm -i postgres:17-alpine psql "$SUPABASE_MIGRATION_URL" -v ON_ERROR_STOP=1 -qAt "$@"; }
if [ -f $f ]; then pw=$(sed -n "s#^DR_STACKSTORM_URL=postgresql://[^:]*:\([^@]*\)@.*#\1#p" $f); else pw=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32); fi
[ ${#pw} -eq 32 ] || { echo "bad pw len for $r"; exit 1; }
psqlm <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='$r') THEN CREATE ROLE $r LOGIN PASSWORD '$pw'; ELSE ALTER ROLE $r LOGIN PASSWORD '$pw'; END IF;
END \$\$;
GRANT dr_ops TO $r;
GRANT dr_automation TO $r;
SQL
printf 'DR_STACKSTORM_URL=postgresql://%s.%s:%s@%s:5432/postgres?sslmode=require\n' "$r" "$REF" "$pw" "$HOST" > $f
chmod 600 $f
echo "ok $r -> dr_ops, dr_automation ($f)"
