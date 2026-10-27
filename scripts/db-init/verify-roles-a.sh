#!/bin/bash
set -uo pipefail
S=/etc/drproj/secrets
q() { # role var sql
  ( . $S/db-$1.env; docker run --rm -i postgres:17-alpine psql "${!2}" -v ON_ERROR_STOP=1 -qAt ) 
}
expect() { # role var label sql  (sql must return t/f)
  local r; r=$(echo "$4" | q "$1" "$2" 2>&1 | tail -1); echo "$1: $3 => $r"; }
A="app_runtime SUPABASE_APP_URL"; O="ops SUPABASE_OPS_URL"; B="backup SUPABASE_BACKUP_URL"; R="restore SUPABASE_RESTORE_URL"; U="automation DR_AUTOMATION_URL"
cap="'ops.capacity_slots'"
# privilege matrix via has_*_privilege (no mutation)
for spec in "$A" "$O" "$B" "$R" "$U"; do set -- $spec
  echo "select current_user||' '||
   'app_rw='||(has_table_privilege('app.relief_requests','SELECT') and has_table_privilege('app.relief_requests','INSERT') and has_table_privilege('app.relief_requests','UPDATE'))||
   ' app_ro='||has_table_privilege('app.relief_requests','SELECT')||
   ' admit_write='||has_function_privilege('ops.app_admit_write()','EXECUTE')||
   ' ops_tbl_select='||has_table_privilege('ops.capacity_slots','SELECT')||
   ' cap_write='||(has_table_privilege('ops.capacity_slots','INSERT') or has_table_privilege('ops.capacity_slots','UPDATE') or has_table_privilege('ops.capacity_slots','DELETE'))||
   ' policy_write='||has_table_privilege('ops.policy_versions','UPDATE')||
   ' app_owner='||(select pg_get_userbyid(nspowner) from pg_namespace where nspname='app')||
   ' can_set_dr_restore='||pg_has_role('dr_restore','USAGE')" | q $1 $2
done
echo "--- live attempts (each rolled back)"
echo "begin; select count(*) from app.relief_requests; select * from ops.app_admit_write(); insert into app.relief_requests(resource_type,quantity,location,urgency) values('food',1,'verify','low'); update app.relief_requests set quantity=2 where location='verify'; rollback;" | q $A 2>&1 | tr '\n' ' '; echo
for stmt in "select count(*) from ops.capacity_slots" "select * from ops.instances limit 1" "insert into ops.keepalive_runs(started_at,succeeded,source) values(now(),true,'x')" "select 1 from ops.runtime_flags"; do
  echo "app_runtime denied? [$stmt] => $(echo "$stmt" | q $A 2>&1 | tail -1 | cut -c1-70)"; done
echo "ops write capacity (expect permission denied): $(echo "update ops.capacity_slots set generation=generation where false" | q $O 2>&1 | tail -1 | cut -c1-80)"
echo "backup write app (expect denied): $(echo "insert into app.relief_requests(resource_type,quantity,location,urgency) values('food',1,'v','low')" | q $B 2>&1 | tail -1 | cut -c1-80)"
echo "backup read app: $(echo "select count(*) from app.relief_requests" | q $B 2>&1 | tail -1)"
echo "backup exec app_admit_write (expect denied): $(echo "select * from ops.app_admit_write()" | q $B 2>&1 | tail -1 | cut -c1-80)"
echo "automation write capacity (rolled back): $(echo "begin; update ops.capacity_slots set generation=generation where false; rollback" | q $U 2>&1 | tr '\n' ' ' | cut -c1-80)"
echo "restore can create in app (rolled back): $(echo "begin; create table app.zz_verify(i int); drop table app.zz_verify; rollback" | q $R 2>&1 | tr '\n' ' ' | cut -c1-90)"
echo "restore rename schema app (rolled back): $(echo "begin; alter schema app rename to app_verify; alter schema app_verify rename to app; rollback" | q $R 2>&1 | tr '\n' ' '| cut -c1-90)"
