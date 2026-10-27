#!/bin/sh
# Guarded Terraform wrapper for one capacity slot (docs/07 "Terraform boundaries").
#   tf-slot.sh PROVIDER SLOT ACTION [-var name=value ...]
#   PROVIDER digitalocean|linode   SLOT one of the 13 capacity slot keys   ACTION plan|apply|output|show
# State: $TF_STATE_DIR/<provider>/<slot>/terraform.tfstate (dir 0700, files 0600), local backend.
# Enforces -lock-timeout=30s; never -lock=false, never destroy (delete = apply -var exists=false).
# Only whitelisted -var names pass through; provider tokens come from the environment, never argv.
# The provider must own the slot: elastic-do-* and interim-warm-digitalocean are DigitalOcean, elastic-linode-* and
# interim-warm-linode are Linode, and `warm` needs WARM_PROVIDER (digitalocean|linode, the capacity.warm_provider policy value)
# equal to PROVIDER for plan/apply. WARM_PROVIDER is passed on as -var warm_provider.
# apply prints "tf-slot: apply started" on stderr just before Terraform apply runs: a failure after that line may have created a
# resource (ambiguous); a failure before it did not touch the provider.
# Each provider has its own root (infra/terraform/slot-do, slot-linode) declaring only that provider, so a DigitalOcean slot never
# loads or configures the Linode provider and vice versa (per-provider failure boundary).
# TERRAFORM overrides the binary (tests use a stub).
set -eu
umask 077

die() { printf 'tf-slot: %s\n' "$1" >&2; exit 2; }

[ $# -ge 3 ] || die "usage: tf-slot.sh PROVIDER SLOT plan|apply|output|show [-var name=value ...]"
provider=$1; slot=$2; action=$3; shift 3

[ -n "${TF_STATE_DIR:-}" ] || die "TF_STATE_DIR is not set"
case $TF_STATE_DIR in /*) ;; *) die "TF_STATE_DIR must be an absolute path" ;; esac

case $provider in digitalocean|linode) ;; *) die "unknown provider" ;; esac
case $slot in
  warm|interim-warm-digitalocean|interim-warm-linode) ;;
  elastic-do-[1-5]|elastic-linode-[1-5]) ;;
  *) die "unknown slot" ;;
esac
case $action in plan|apply|output|show) ;; *) die "action must be plan, apply, output or show (destroy is not allowed)" ;; esac

case $slot in
  elastic-do-*|interim-warm-digitalocean) owner=digitalocean ;;
  elastic-linode-*|interim-warm-linode) owner=linode ;;
  warm) owner= ;;
esac
if [ -n "$owner" ]; then
  [ "$provider" = "$owner" ] || die "slot $slot belongs to $owner, not $provider"
fi
warm_var=
case $action in
  plan|apply)
    if [ -n "${WARM_PROVIDER:-}" ]; then
      case $WARM_PROVIDER in digitalocean|linode) ;; *) die "WARM_PROVIDER must be digitalocean or linode" ;; esac
      warm_var=$WARM_PROVIDER
    fi
    if [ "$slot" = warm ]; then
      [ -n "$warm_var" ] || die "WARM_PROVIDER is required for the warm slot"
      [ "$warm_var" = "$provider" ] || die "warm slot provider $provider differs from WARM_PROVIDER $warm_var"
    fi ;;
esac

# Validate the remaining args as -var pairs, rotating them through the positional list.
n=$#
while [ "$n" -gt 0 ]; do
  [ "$1" = "-var" ] || die "argument not allowed: $1"
  [ $# -ge 2 ] || die "-var needs a value"
  case $2 in
    exists=*|op=*|attempt=*|region=*|size=*|image=*|ssh_key_id=*) ;;
    *) die "variable not allowed: ${2%%=*}" ;;
  esac
  set -- "$@" "$1" "$2"; shift 2
  n=$((n - 2))
done

case $provider in
  digitalocean) rootname="slot-do" ;;
  linode) rootname="slot-linode" ;;
esac
root=$(CDPATH="" cd -- "$(dirname -- "$0")/../infra/terraform/$rootname" && pwd)
tf=${TERRAFORM:-terraform}
slotdir=$TF_STATE_DIR/$provider/$slot
state=$slotdir/terraform.tfstate

install -d -m 0700 "$TF_STATE_DIR" "$TF_STATE_DIR/$provider" "$slotdir" "$slotdir/backups"

[ -z "$warm_var" ] || set -- "$@" -var "warm_provider=$warm_var"

cd "$root"
"$tf" init -input=false -reconfigure -backend-config="path=$state" >&2

case $action in
  plan)
    exec "$tf" plan -input=false -lock-timeout=30s \
      -var "provider_name=$provider" -var "slot=$slot" "$@" ;;
  apply)
    if [ -f "$state" ]; then
      backup=$slotdir/backups/$(date -u +%Y%m%dT%H%M%SZ).tfstate
      cp "$state" "$backup"
      chmod 0600 "$backup"
    fi
    printf 'tf-slot: apply started\n' >&2
    exec "$tf" apply -input=false -auto-approve -lock-timeout=30s \
      -var "provider_name=$provider" -var "slot=$slot" "$@" ;;
  output)
    exec "$tf" output -json ;;
  show)
    exec "$tf" show -json ;;
esac
