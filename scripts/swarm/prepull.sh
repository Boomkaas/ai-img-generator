#!/bin/sh
set -eu

if [ $# -ne 1 ]; then
    echo "usage: REGISTRY=<registry> $0 <tag>" >&2
    exit 2
fi
: "${REGISTRY:?set REGISTRY to the same value as in infra/.env.prod}"

docker pull "$REGISTRY/imagegen-worker:$1"
docker pull "$REGISTRY/imagegen-api:$1"
