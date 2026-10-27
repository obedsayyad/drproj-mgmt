#!/bin/bash
# Static checks for monitoring config using the pinned upstream images (no stack needed):
#   promtool check config / check rules / test rules, amtool check-config.
set -eu
MGMT=$(cd "$(dirname "$0")/../.." && pwd)
C=$MGMT/deploy/compose/mgmt
tag() { sed -n "s/.*$1:-\([^}]*\)}.*/\1/p" "$C/compose.yaml" | head -1; }
PROM=prom/prometheus:v$(tag PROMETHEUS_TAG); AM=prom/alertmanager:v$(tag ALERTMANAGER_TAG)
T=$(mktemp -d "$MGMT/.promtool.XXXXXX"); chmod 755 "$T";  # under the repo: Docker Desktop shares /Users, not /var/folders
trap 'rm -rf "$T"' EXIT
cp "$C/prometheus/prometheus.yml" "$T/"; mkdir "$T/rules" "$T/targets"; cp -R "$MGMT/monitoring/rules/." "$T/rules/"; cp -R "$MGMT/monitoring/targets/." "$T/targets/"
# prometheus.yml references /etc/prometheus/{rules,targets}; mount the same layout.
# Directory mounts only (single-file bind mounts from the macOS temp dir fail on Docker Desktop).
docker run --rm -v "$T:/etc/prometheus:ro" --entrypoint promtool "$PROM" check config /etc/prometheus/prometheus.yml
# What cdr.apply_policy writes on activation: the same file with another policy_version must still be a valid config (stdlib python, no pack install).
python3 "$MGMT/automation/stackstorm/packs/cdr/actions/lib/prom_render.py" "$T/prometheus.yml" 7 "$T/prometheus.rendered.yml"
grep -q 'policy_version: "7"' "$T/prometheus.rendered.yml" || { echo "policy_version label was not re-rendered" >&2; exit 1; }
cp "$T/prometheus.yml" "$T/prometheus.orig.yml"; cp "$T/prometheus.rendered.yml" "$T/prometheus.yml"
docker run --rm -v "$T:/etc/prometheus:ro" --entrypoint promtool "$PROM" check config /etc/prometheus/prometheus.yml
cp "$T/prometheus.orig.yml" "$T/prometheus.yml"
docker run --rm -v "$T/rules:/r:ro" --entrypoint promtool "$PROM" test rules /r/tests/cdr.test.yml
mkdir "$T/am" "$T/secrets"; cp "$C/alertmanager/alertmanager.yml" "$T/am/"; printf 'dummy-key\n' >"$T/secrets/st2_api_key"
docker run --rm -v "$T/am:/am:ro" -v "$T/secrets:/run/secrets:ro" --entrypoint amtool "$AM" check-config /am/alertmanager.yml
