#!/usr/bin/env bats
# Guards of scripts/tf-slot.sh, run against a stub terraform that records its argv.

bats_require_minimum_version 1.5.0

setup() {
  SCRIPT="$BATS_TEST_DIRNAME/../../scripts/tf-slot.sh"
  WORK=$(mktemp -d)
  export TF_STATE_DIR="$WORK/state"
  export CALLS="$WORK/calls"
  export TERRAFORM="$WORK/terraform"
  cat > "$TERRAFORM" <<'STUB'
#!/bin/sh
echo "$*" >> "$CALLS"
if [ "$1" = init ] && [ -n "${FAIL_INIT:-}" ]; then exit 1; fi
# at apply time the pre-apply backup must already exist
if [ "$1" = apply ]; then ls "$TF_STATE_DIR"/*/*/backups > "$CALLS.backups" 2>/dev/null; fi
exit 0
STUB
  chmod 755 "$TERRAFORM"
}

teardown() { rm -rf "$WORK"; }

mode() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"; }

@test "refuses when TF_STATE_DIR is unset" {
  unset TF_STATE_DIR
  run "$SCRIPT" digitalocean warm plan
  [ "$status" -eq 2 ]
  [[ "$output" == *TF_STATE_DIR* ]]
  [ ! -e "$CALLS" ]
}

@test "refuses a relative TF_STATE_DIR" {
  TF_STATE_DIR=state run "$SCRIPT" digitalocean warm plan
  [ "$status" -eq 2 ]
}

@test "refuses destroy and unknown actions" {
  run "$SCRIPT" digitalocean warm destroy
  [ "$status" -eq 2 ]
  run "$SCRIPT" digitalocean warm import
  [ "$status" -eq 2 ]
  [ ! -e "$CALLS" ]
}

@test "refuses -lock=false and any non-var argument" {
  run "$SCRIPT" digitalocean warm apply -lock=false
  [ "$status" -eq 2 ]
  run "$SCRIPT" digitalocean warm apply -destroy
  [ "$status" -eq 2 ]
  run "$SCRIPT" digitalocean warm apply -var lock=false
  [ "$status" -eq 2 ]
  [ ! -e "$CALLS" ]
}

@test "rejects unknown provider and slot keys" {
  run "$SCRIPT" aws warm plan
  [ "$status" -eq 2 ]
  run "$SCRIPT" linode elastic-linode-6 plan
  [ "$status" -eq 2 ]
  run "$SCRIPT" linode ../escape plan
  [ "$status" -eq 2 ]
}

@test "apply: lock timeout, provider in backend path, 0700 dirs" {
  run "$SCRIPT" linode elastic-linode-2 apply -var exists=true -var attempt=1
  [ "$status" -eq 0 ]
  grep -q -- "-backend-config=path=$TF_STATE_DIR/linode/elastic-linode-2/terraform.tfstate" "$CALLS"
  grep -q -- "apply .*-lock-timeout=30s" "$CALLS"
  grep -q -- "-var provider_name=linode" "$CALLS"
  ! grep -q -- "-lock=false" "$CALLS"
  [ "$(mode "$TF_STATE_DIR/linode/elastic-linode-2")" = 700 ]
  [ "$(mode "$TF_STATE_DIR")" = 700 ]
}

@test "apply backs up an existing state file 0600 before terraform runs" {
  mkdir -p "$TF_STATE_DIR/digitalocean/warm"
  echo '{"serial":1}' > "$TF_STATE_DIR/digitalocean/warm/terraform.tfstate"
  WARM_PROVIDER=digitalocean run "$SCRIPT" digitalocean warm apply -var exists=false
  [ "$status" -eq 0 ]
  [ -s "$CALLS.backups" ]
  b=$(ls "$TF_STATE_DIR"/digitalocean/warm/backups/*.tfstate)
  [ "$(mode "$b")" = 600 ]
  grep -q serial "$b"
}

@test "refuses fewer than three arguments" {
  run "$SCRIPT"
  [ "$status" -eq 2 ]
  run "$SCRIPT" digitalocean
  [ "$status" -eq 2 ]
  run "$SCRIPT" digitalocean elastic-do-1
  [ "$status" -eq 2 ]
  [[ "$output" == *usage* ]]
  [ ! -e "$CALLS" ]
}

@test "refuses a dangling -var and a -var for a name that is not whitelisted" {
  run "$SCRIPT" digitalocean elastic-do-1 apply -var
  [ "$status" -eq 2 ]
  run "$SCRIPT" digitalocean elastic-do-1 apply -var exists=true -var
  [ "$status" -eq 2 ]
  run "$SCRIPT" digitalocean elastic-do-1 apply -var warm_provider=linode
  [ "$status" -eq 2 ]
  [ ! -e "$CALLS" ]
}

@test "refuses a provider that does not own the slot, in both directions" {
  run "$SCRIPT" linode elastic-do-1 plan
  [ "$status" -eq 2 ]
  run "$SCRIPT" digitalocean elastic-linode-1 plan
  [ "$status" -eq 2 ]
  run "$SCRIPT" digitalocean interim-warm-linode apply -var exists=true
  [ "$status" -eq 2 ]
  run "$SCRIPT" linode interim-warm-digitalocean apply -var exists=true
  [ "$status" -eq 2 ]
  [[ "$output" == *"belongs to digitalocean"* ]]
  [ ! -e "$CALLS" ]
}

@test "the warm slot needs WARM_PROVIDER equal to the provider" {
  unset WARM_PROVIDER
  run "$SCRIPT" digitalocean warm apply -var exists=true
  [ "$status" -eq 2 ]
  [[ "$output" == *WARM_PROVIDER* ]]
  WARM_PROVIDER=linode run "$SCRIPT" digitalocean warm apply -var exists=true
  [ "$status" -eq 2 ]
  WARM_PROVIDER=aws run "$SCRIPT" digitalocean warm plan
  [ "$status" -eq 2 ]
  [ ! -e "$CALLS" ]
  WARM_PROVIDER=linode run "$SCRIPT" linode warm plan
  [ "$status" -eq 0 ]
  grep -q -- "-var warm_provider=linode" "$CALLS"
}

@test "plan: lock timeout, provider, slot and caller vars, no apply" {
  run "$SCRIPT" linode elastic-linode-4 plan -var exists=true -var attempt=2
  [ "$status" -eq 0 ]
  grep -q -- "^plan -input=false -lock-timeout=30s -var provider_name=linode -var slot=elastic-linode-4 -var exists=true -var attempt=2" "$CALLS"
  ! grep -q "^apply" "$CALLS"
  ! grep -q -- "warm_provider" "$CALLS"
  [ ! -e "$WORK/state/linode/elastic-linode-4/backups"/*.tfstate ]
}

@test "output and show read the state and take no -var" {
  run "$SCRIPT" digitalocean elastic-do-5 output
  [ "$status" -eq 0 ]
  grep -q "^output -json$" "$CALLS"
  run "$SCRIPT" digitalocean elastic-do-5 show
  [ "$status" -eq 0 ]
  grep -q "^show -json$" "$CALLS"
  ! grep -q "^apply\|^plan" "$CALLS"
  run "$SCRIPT" digitalocean elastic-do-5 output -var exists=true
  [ "$status" -eq 0 ]
  ! grep -q -- "-var" <(grep "^output" "$CALLS")
}

@test "apply prints the apply-started marker only after init succeeded and the guards passed" {
  run --separate-stderr "$SCRIPT" digitalocean elastic-do-1 apply -var exists=true
  [ "$status" -eq 0 ]
  [[ "$stderr" == *"tf-slot: apply started"* ]]
  FAIL_INIT=1 run --separate-stderr "$SCRIPT" digitalocean elastic-do-1 apply -var exists=true
  [ "$status" -ne 0 ]
  [[ "$stderr" != *"apply started"* ]]
  run --separate-stderr "$SCRIPT" digitalocean elastic-linode-1 apply -var exists=true
  [ "$status" -eq 2 ]
  [[ "$stderr" != *"apply started"* ]]
  run --separate-stderr "$SCRIPT" digitalocean elastic-do-1 plan
  [[ "$stderr" != *"apply started"* ]]
}

@test "each provider runs terraform in its own root, never the other's" {
  # the stub records its working directory next to argv
  cat > "$TERRAFORM" <<'STUB'
#!/bin/sh
echo "$(basename "$PWD") $*" >> "$CALLS"
exit 0
STUB
  run "$SCRIPT" linode elastic-linode-2 apply -var exists=true
  [ "$status" -eq 0 ]
  WARM_PROVIDER=linode run "$SCRIPT" linode warm plan
  grep -q "^slot-linode " "$CALLS"
  ! grep -q "^slot-do " "$CALLS"
  rm -f "$CALLS"
  run "$SCRIPT" digitalocean elastic-do-2 apply -var exists=true
  [ "$status" -eq 0 ]
  run "$SCRIPT" digitalocean elastic-do-2 output
  run "$SCRIPT" digitalocean elastic-do-2 show
  grep -q "^slot-do " "$CALLS"
  ! grep -q "^slot-linode " "$CALLS"
  [ "$(grep -vc '^slot-do ' "$CALLS")" -eq 0 ]
}

@test "no slot root declares both providers and the combined root is gone" {
  root="$BATS_TEST_DIRNAME/../../infra/terraform"
  [ ! -e "$root/slot" ]
  grep -q 'digitalocean/digitalocean' "$root/slot-do/versions.tf"
  ! grep -q 'linode' "$root/slot-do/versions.tf"
  ! grep -q 'modules/linode_instance' "$root/slot-do/main.tf"
  grep -q 'linode/linode' "$root/slot-linode/versions.tf"
  ! grep -qi 'digitalocean' "$root/slot-linode/versions.tf"
  ! grep -q 'modules/do_droplet' "$root/slot-linode/main.tf"
}
