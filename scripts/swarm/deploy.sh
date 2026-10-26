#!/bin/sh
# Run on the manager: deploys (or updates) the stack with the images tagged <tag>.
#
#   scripts/swarm/deploy.sh <tag> [extra stack file ...]
#   scripts/swarm/deploy.sh 1.0.0
#   scripts/swarm/deploy.sh dev infra/stack.dev-swarm.yml      (one-machine smoke run)
#
# Settings come from infra/.env.prod (ENV_FILE overrides the path); copy it from infra/.env.prod.example.
# The passwords must already be Swarm secrets (scripts/swarm/create-secrets.sh).
set -eu

if [ $# -lt 1 ]; then
    echo "usage: $0 <tag> [extra stack file ...]" >&2
    exit 2
fi

# Paths given on the command line are relative to where the script was started, not to the repository.
absolute() {
    case $1 in
        /*) printf '%s' "$1" ;;
        *) printf '%s/%s' "$PWD" "$1" ;;
    esac
}

repo=$(cd "$(dirname "$0")/../.." && pwd)
env_file=$(absolute "${ENV_FILE:-$repo/infra/.env.prod}")
if [ ! -f "$env_file" ]; then
    echo "$env_file not found; copy infra/.env.prod.example and fill it in" >&2
    exit 1
fi
set -a
. "$env_file"
set +a
TAG=$1
export TAG
shift

for name in REGISTRY DOMAIN; do
    eval "value=\${$name:-}"
    if [ -z "$value" ]; then
        echo "$name is empty in $env_file" >&2
        exit 1
    fi
done

files="-c $repo/infra/stack.yml"
for extra in "$@"; do
    files="$files -c $(absolute "$extra")"
done
# The stack name is fixed: the controller scales imagegen_worker.
docker stack deploy $files imagegen --with-registry-auth
