#!/bin/bash
# T31 (operator auth) plus the P3.1 Compose hardening checks, run LOCALLY under Docker Desktop.
# This is NOT the management VM: Caddy uses its internal CA on loopback, Postgres is a throwaway container,
# StackStorm/Prometheus/Grafana are not started. Output is recorded in tests/compose/t31.local.out.
#
#   tests/compose/t31.sh            # writes tests/compose/t31.local.out
# Needs: docker, pnpm, node, curl, openssl. Set COMPOSE_BIN if `docker compose` is unavailable.
set -u -o pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
MGMT=$(cd "$HERE/../.." && pwd)
APP=$(cd "$MGMT/../drproj-app" && pwd)
OUT=${T31_OUT:-$HERE/t31.local.out}
PORT=${T31_HTTPS_PORT:-18443}
PGPORT=${T31_PG_PORT:-55433}
PROJECT=drproj-t31

if docker compose version >/dev/null 2>&1; then DC=(docker compose); else DC=(${COMPOSE_BIN:-docker-compose}); fi

TMP=$(mktemp -d); chmod 700 "$TMP"
PASS=0; FAILN=0
exec > >(tee "$OUT") 2>&1

say() { printf '%s\n' "$*"; }
pass() { PASS=$((PASS + 1)); say "PASS $*"; }
fail() { FAILN=$((FAILN + 1)); say "FAIL $*"; }
expect() { if [ "$2" = "$3" ]; then pass "$1 ($2)"; else fail "$1 (got $2, want $3)"; fi; }

cleanup() {
  say "--- cleanup"
  (cd "$MGMT/deploy/compose/mgmt" && "${DC[@]}" -p "$PROJECT" --env-file "$TMP/mgmt.env" -f compose.yaml -f compose.local.yaml --profile local down -v --remove-orphans >/dev/null 2>&1)
  rm -rf "$TMP"
}
trap cleanup EXIT

say "T31 LOCAL RUN - Docker Desktop on a developer Mac, NOT the management VM"
say "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ---- secrets for this run (random, never printed) ----
OPS_PW=$(openssl rand -hex 16)
PG_PW=$(openssl rand -hex 12)
CADDY_TAG=$(sed -n 's/.*CADDY_TAG:-\([^}]*\)}.*/\1/p' "$MGMT/deploy/compose/mgmt/compose.yaml" | head -1)
HASH=$(docker run --rm "caddy:$CADDY_TAG" caddy hash-password --plaintext "$OPS_PW")
umask 077
cat >"$TMP/mgmt.env" <<ENV
SUPABASE_OPS_URL=postgres://ops_t31:${PG_PW}@localpg:5432/postgres
OPS_OPERATOR_PASSWORD=${OPS_PW}
OPS_BASIC_AUTH_HASH='${HASH}'
MONGO_ROOT_USER=unused
MONGO_ROOT_PASSWORD=unused
RABBITMQ_USER=unused
RABBITMQ_PASSWORD=unused
RESTORE_TEST_PG_PASSWORD=unused
LOCAL_PG_PASSWORD=${PG_PW}
ACME_EMAIL=
T31_HTTPS_PORT=${PORT}
T31_PG_PORT=${PGPORT}
ENV
umask 022

say "--- 1. permissions helper"
"$MGMT/scripts/check-perms.sh" "$TMP/mgmt.env" >/dev/null && pass "check-perms accepts a 0600 .env in a 0700 dir" || fail "check-perms rejected the run's .env"
chmod 644 "$TMP/mgmt.env"
"$MGMT/scripts/check-perms.sh" "$TMP/mgmt.env" >/dev/null && fail "check-perms accepted a 0644 .env" || pass "check-perms rejects a 0644 .env"
chmod 600 "$TMP/mgmt.env"

say "--- 2. compose config validates and meets hardening rules"
cfg() { # role dir [extra compose args...]
  local role=$1 dir=$2; shift 2
  (cd "$MGMT/$dir" && "${DC[@]}" --env-file "$TMP/mgmt.env" "$@" config --format json 2>"$TMP/cfg.err") | node "$HERE/assert-config.mjs" "$role" && return 0
  [ -s "$TMP/cfg.err" ] && sed 's/^/    /' "$TMP/cfg.err" | head -5
  return 1
}
cfg mgmt deploy/compose/mgmt --profile '*' -f compose.yaml && pass "mgmt compose" || fail "mgmt compose"
cfg mgmt deploy/compose/mgmt --profile '*' -f compose.yaml -f compose.local.yaml && pass "mgmt compose + local overlay" || fail "mgmt compose + local overlay"
cfg testing deploy/compose/testing --profile '*' && pass "testing compose" || fail "testing compose"
touch "$TMP/empty.env"
for role in app lb; do
  (cd "$APP/deploy/compose/$role" && touch .env && "${DC[@]}" --profile '*' config --format json 2>"$TMP/cfg.err"; rm -f .env) | node "$HERE/assert-config.mjs" "$role" && pass "$role compose" || fail "$role compose"
done

say "--- 3. bring up local Postgres, migrate, seed policy"
(cd "$MGMT" && docker build -q -f apps/operations/Dockerfile -t drproj-operations:local . >/dev/null) && pass "ops image builds (local tag, not pushed)" || fail "ops image build"
cd "$MGMT/deploy/compose/mgmt" || exit 1
"${DC[@]}" -p "$PROJECT" --env-file "$TMP/mgmt.env" -f compose.yaml -f compose.local.yaml --profile local up -d --wait localpg >/dev/null 2>&1 && pass "localpg healthy" || { fail "localpg did not start"; exit 1; }
SUPABASE_MIGRATION_URL="postgres://postgres:${PG_PW}@127.0.0.1:${PGPORT}/postgres" pnpm --dir "$APP" -s migrate 2>&1 | sed 's/^/    /' | tail -3
LPG=$("${DC[@]}" -p "$PROJECT" --env-file "$TMP/mgmt.env" -f compose.yaml -f compose.local.yaml --profile local ps -q localpg)
docker exec "$LPG" psql -U postgres -v ON_ERROR_STOP=1 -qc "CREATE ROLE ops_t31 LOGIN PASSWORD '${PG_PW}' IN ROLE dr_ops" && pass "dr_ops login role created" || fail "login role"
(cd "$MGMT" && SUPABASE_OPS_URL="postgres://ops_t31:${PG_PW}@127.0.0.1:${PGPORT}/postgres" pnpm -s seed:policy 2>&1 | sed 's/^/    /' | tail -2)

say "--- 4. start ops + caddy"
"${DC[@]}" -p "$PROJECT" --env-file "$TMP/mgmt.env" -f compose.yaml -f compose.local.yaml --profile local up -d --wait ops caddy >/dev/null 2>&1 && pass "ops and caddy healthy" || { fail "ops/caddy did not become healthy"; "${DC[@]}" -p "$PROJECT" --env-file "$TMP/mgmt.env" -f compose.yaml -f compose.local.yaml logs --tail 15 ops caddy 2>&1 | sed 's/^/    /' | sed "s/${PG_PW}/***/g"; exit 1; }

say "--- 5. T31 through Caddy (https://ops.localhost:${PORT}, internal CA)"
curlc() { curl -ksS -o /dev/null -D "$TMP/h" -w '%{http_code}' --max-time 10 --resolve "ops.localhost:${PORT}:127.0.0.1" "$@"; }
URL="https://ops.localhost:${PORT}"
expect "GET /api/overview without credentials" "$(curlc "$URL/api/overview")" 200
expect "GET /health/live" "$(curlc "$URL/health/live")" 200
for m in POST PATCH PUT DELETE; do
  code=$(curlc -X "$m" -H 'content-type: application/json' -d '{}' "$URL/api/operations")
  expect "$m without credentials" "$code" 401
  grep -qi '^www-authenticate: *basic' "$TMP/h" && pass "$m 401 carries WWW-Authenticate: Basic" || fail "$m 401 lacks WWW-Authenticate"
  grep -qi 'realm="restricted"' "$TMP/h" && pass "$m 401 is issued by Caddy (realm restricted)" || fail "$m 401 not from Caddy"
done
expect "POST with wrong password" "$(curlc -u operator:wrong-password-x -X POST -H 'content-type: application/json' -d '{}' "$URL/api/operations")" 401
for m in POST PATCH PUT DELETE; do
  code=$(curlc -u "operator:${OPS_PW}" -X "$m" -H 'content-type: application/json' -d '{}' "$URL/api/operations")
  if [ "$code" != 401 ] && [ "$code" != 000 ]; then pass "$m with credentials is not 401 ($code)"; else fail "$m with credentials ($code)"; fi
done

say "--- 6. defense in depth: the API rejects mutations itself (no Caddy)"
OPSC=$("${DC[@]}" -p "$PROJECT" --env-file "$TMP/mgmt.env" -f compose.yaml -f compose.local.yaml ps -q ops)
direct=$(docker exec "$OPSC" node -e "fetch('http://127.0.0.1:8080/api/operations',{method:'POST',headers:{'content-type':'application/json'},body:'{}'}).then(r=>console.log(r.status+' '+r.headers.get('www-authenticate')))")
expect "direct POST to ops container without credentials" "${direct%% *}" 401

say "--- 7. ops /metrics is internal-only"
mcode=$(docker exec "$OPSC" node -e "fetch('http://127.0.0.1:9465/metrics').then(async r=>console.log(r.status+' '+(await r.text()).split('\n').filter(l=>/^cdr_(policy_active_version|ops_metrics_query_success)/.test(l)).join('|')))")
say "    in-container: $mcode"
case "$mcode" in "200 "*cdr_policy_active_version*) pass "/metrics served on 9465 with cdr_* series" ;; *) fail "/metrics" ;; esac
# The dashboard answers unknown GET paths with its SPA index.html, so check the body, not the status.
viaCaddy=$(curl -ks --max-time 10 --resolve "ops.localhost:${PORT}:127.0.0.1" "$URL/metrics")
case "$viaCaddy" in *cdr_*|*"# TYPE"*) fail "metrics series are reachable through Caddy" ;; *) pass "GET /metrics through Caddy returns no metrics series" ;; esac
published=$(docker port "$OPSC" 2>/dev/null | wc -l | tr -d ' ')
expect "ops container publishes no host ports" "$published" 0

say "--- summary"
say "passed=$PASS failed=$FAILN"
say "images: caddy:${CADDY_TAG} $(docker inspect -f '{{.Config.Image}}' "$OPSC") postgres:$(docker inspect -f '{{.Config.Image}}' "$LPG" | cut -d: -f2)"
[ "$FAILN" -eq 0 ]
