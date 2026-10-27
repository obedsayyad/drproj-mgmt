S=/etc/drproj/secrets
q() { ( . $S/db-$1.env; docker run --rm -i postgres:17-alpine psql "${!2}" -qAt 2>&1 ); }
echo "automation acquire_lease+reserve_slot via fn (rolled back):"
echo "begin; select 'lease_gen='||generation from ops.acquire_lease(gen_random_uuid()); select current_user; select count(*) from ops.capacity_slots; rollback;" | q automation DR_AUTOMATION_URL
echo "ops exec acquire_lease (expect denied):"; echo "select * from ops.acquire_lease(gen_random_uuid())" | q ops SUPABASE_OPS_URL | head -1
echo "ops write ops.audit_events (rolled back):"; echo "begin; insert into ops.audit_events(source,action,object) values('verify','verify','x'); rollback; select 'ok'" | q ops SUPABASE_OPS_URL | tail -1
echo "ops read app schema (expect denied):"; echo "select count(*) from app.relief_requests" | q ops SUPABASE_OPS_URL | head -1
echo "backup exec admit (expect denied):"; echo "select * from ops.app_admit_write()" | q backup SUPABASE_BACKUP_URL | head -1
echo "backup read ops (expect denied):"; echo "select count(*) from ops.policy_versions" | q backup SUPABASE_BACKUP_URL | head -1
echo "restore ops access (expect denied):"; echo "select count(*) from ops.policy_versions" | q restore SUPABASE_RESTORE_URL | head -1
echo "automation read app (expect denied):"; echo "select count(*) from app.relief_requests" | q automation DR_AUTOMATION_URL | head -1
echo "automation read policy:"; echo "select count(*) from ops.policy_versions" | q automation DR_AUTOMATION_URL | head -1
echo "app_runtime ddl (expect denied):"; echo "create table app.zz(i int)" | q app_runtime SUPABASE_APP_URL | head -1
echo "prepared stmts / pooler: server $(echo 'show server_version' | q ops SUPABASE_OPS_URL)"
