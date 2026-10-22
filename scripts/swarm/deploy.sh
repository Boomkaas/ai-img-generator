#!/bin/sh
# Run on the manager: deploys (or updates) the stack with the images tagged <tag>.
#
#   scripts/swarm/deploy.sh <tag> [extra stack file ...]
#   scripts/swarm/deploy.sh 1.0.0
#   scripts/swarm/deploy.sh dev infra/stack.dev-swarm.yml      (one-machine smoke run)
#
# Settings and secrets come from infra/.env.prod (ENV_FILE overrides the path); copy it from
# infra/.env.prod.example.
set -eu

if [ $# -lt 1 ]; then
    echo "usage: $0 <tag> [extra stack file ...]" >&2
    exit 2
fi
cd "$(dirname "$0")/../.."

env_file=${ENV_FILE:-infra/.env.prod}
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

for name in REGISTRY DOMAIN REDIS_PASSWORD PG_PASSWORD MINIO_PASSWORD JWT_SECRET; do
    eval "value=\${$name:-}"
    if [ -z "$value" ]; then
        echo "$name is empty in $env_file" >&2
        exit 1
    fi
done

files="-c infra/stack.yml"
for extra in "$@"; do
    files="$files -c $extra"
done
# The stack name is fixed: the controller scales imagegen_worker.
docker stack deploy $files imagegen --with-registry-auth
