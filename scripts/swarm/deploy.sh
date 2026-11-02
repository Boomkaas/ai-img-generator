#!/bin/sh
set -eu

if [ $# -lt 1 ]; then
    echo "usage: $0 <tag> [extra stack file ...]" >&2
    exit 2
fi

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
docker stack deploy $files imagegen --with-registry-auth
