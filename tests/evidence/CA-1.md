# CA-1 evidence: backend CA init, primary leaf, bundle distribution

Run as root on the management VM (`/opt/drproj/ansible`, venv `/opt/drproj/ansible-venv`), 2026-10-27 ~21:14Z. No secrets appear here.
Status: **partial** — CA, leaf, bundle and perms are done; the node_exporter `/metrics` check could not pass because node_exporter is not running yet.

## ca-init.yml --check --diff

Would create `/etc/drproj/ca` and generate the key. It then failed on "Restrict the CA private key" because the key does not exist in check mode (check-mode limitation, same class as `docker-ce` in BOOTSTRAP-1). The later self-sign and bundle tasks were therefore not previewed. The role was not changed.

## ca-init.yml (real)

ok=8 changed=5 failed=0. `/etc/drproj/ca`: `ca.key` 0400 root, `ca.crt` and `ca-bundle.crt` 0644, directory 0700. CA subject `CN=drproj-backend-ca`, P-256, 3650 days.

## ca-issue.yml (primary)

ok=18 changed=9 failed=0. The leaf key was generated on primary; only the CSR travelled. Signing used the CA on management; `openssl verify` passed. Installed `/etc/drproj/app/certs/app.{crt,key}` (0644 inside a 0700 directory, per role). Work directory on management removed.

## Leaf check (primary)

- subject `CN=primary-vm`, issuer `CN=drproj-backend-ca`
- notBefore 2026-10-27 21:14:42Z, notAfter 2027-01-25 21:14:42Z (90 days)
- SAN `IP:103.141.79.171, DNS:app.cproj.obed.one`; EKU serverAuth
- `openssl verify -CAfile /etc/drproj/trust/ca-bundle.crt app.crt` → OK

## Bundle distribution

`ca-issue.yml` does not distribute the bundle (only the rotate plays include `distribute_bundle.yml`). I ran the role's `distribute_bundle` tasks through a throwaway play (deleted afterwards) on `primary` and `lb`. Both hold `/etc/drproj/trust/ca-bundle.crt` (0644) with SHA-256 6cbf34f0…ea45b8, the same as management's bundle.

## check-perms.sh (management)

`BACKUP_DIR=/var/backups/drproj TFSTATE_DIR=/var/lib/drproj/terraform /opt/drproj/scripts/check-perms.sh` → both `ok ... 700`, rc=0. Both directories were absent and were created 0700 (`/var/lib/drproj` is also 0700). `scripts/check-perms.sh` was copied to `/opt/drproj/scripts/` (not part of the rsync'd Ansible tree).

## node_exporter /metrics — NOT VERIFIED

`curl -sk https://103.141.79.171:9100/metrics` → HTTP 000 (connection refused). Primary has no containers and nothing listens on 9100: node_exporter belongs to the app compose stack, which is not deployed. The web config's `/etc/node_exporter/tls.{crt,key}` are container paths, so they are expected to be mounted from `/etc/drproj/app/certs/app.{crt,key}` by that stack. Re-run the basic-auth curl after the app stack is up.
