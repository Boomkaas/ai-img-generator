#!/bin/sh
# Run on every app machine before the demo. The worker image holds the model weights (several GB); without
# it in advance, the first worker the autoscaler starts here spends minutes downloading instead of loading.
#
#   REGISTRY=registry.example.org scripts/swarm/prepull.sh <tag>
set -eu

if [ $# -ne 1 ]; then
    echo "usage: REGISTRY=<registry> $0 <tag>" >&2
    exit 2
fi
: "${REGISTRY:?set REGISTRY to the same value as in infra/.env.prod}"

docker pull "$REGISTRY/imagegen-worker:$1"
docker pull "$REGISTRY/imagegen-api:$1"
