#!/bin/sh
# usage: dbrun.sh <repo> <envfile...> -- <cmd>
repo=$1; shift
envs=""
while [ "$1" != "--" ]; do envs="$envs --env-file $1"; shift; done; shift
exec docker run --rm $envs -v /opt/drproj/src/$repo:/w -w /w node:22-bookworm-slim sh -c "corepack enable >/dev/null 2>&1; $*"
