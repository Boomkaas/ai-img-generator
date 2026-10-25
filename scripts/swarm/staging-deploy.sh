#!/bin/sh
# Run on the staging manager by the staging workflow, which copies it there first, so checking out another
# commit below can't change the script while it runs:
#
#   sh staging-deploy.sh <repository dir> <commit> <tag> [url]
#
# Checks out <commit> in the repository clone, deploys the images tagged <tag> with the one-machine override,
# waits until every service runs them, and runs scripts/smoke.py against <url> (default https://localhost).
# If any of that fails, it deploys the commit and tag that ran before, and exits 1.
#
# The clone needs infra/.env.prod with REGISTRY pointing at the registry the workflow pushes to, and the
# machine must be able to pull from it (docker login).
set -eu

if [ $# -lt 3 ]; then
    echo "usage: $0 <repository dir> <commit> <tag> [url]" >&2
    exit 2
fi
dir=$1
commit=$2
tag=$3
url=${4:-https://localhost}
stack=imagegen
start_within=300

cd "$dir"

running_tag() {
    docker service inspect -f '{{.Spec.TaskTemplate.ContainerSpec.Image}}' "${stack}_api" 2> /dev/null \
        | sed 's/@.*//; s/.*://'
}

wait_for() {  # until the migrations of <tag> ran and each service runs <tag> and nothing else
    started=$(date +%s)
    while [ $(($(date +%s) - started)) -le "$start_within" ]; do
        settled=yes
        for service in api worker controller nginx; do
            states=$(docker service ps "${stack}_$service" --filter desired-state=running \
                --format '{{.Image}} {{.CurrentState}}')
            fresh=$(echo "$states" | grep -cE ":$1(-fake)?(@[^ ]*)? Running" || true)
            other=$(echo "$states" | grep -cvE ":$1(-fake)?(@[^ ]*)? Running" || true)
            if [ "$fresh" -eq 0 ] || [ "$other" -gt 0 ]; then settled=no; fi
        done
        # The API answers /api/health before the tables exist; the one-off migrate service creates them.
        migrated=$(docker service ps "${stack}_migrate" --format '{{.Image}} {{.CurrentState}}' \
            | grep -cE ":$1(@[^ ]*)? Complete" || true)
        [ "$migrated" -gt 0 ] || settled=no
        [ "$settled" = yes ] && return 0
        sleep 5
    done
    echo "the services did not all run $1 within $start_within s" >&2
    docker stack ps "$stack" --filter desired-state=running --no-trunc >&2 || true
    return 1
}

deploy() {  # deploy <commit> <tag>
    echo "--- deploying $2 ($1)"
    git fetch --quiet origin \
        && git checkout --quiet --detach "$1" \
        && scripts/swarm/deploy.sh "$2" infra/stack.dev-swarm.yml \
        && wait_for "$2"
}

previous=$(running_tag || true)
if deploy "$commit" "$tag" && python3 scripts/smoke.py "$url" --insecure; then
    echo "staging runs $tag"
    exit 0
fi

echo "--- $tag failed on staging" >&2
if [ -n "$previous" ] && [ "$previous" != "$tag" ]; then
    # The tag is sha-<commit>, so the stack files come from the same commit as the images.
    deploy "${previous#sha-}" "$previous" && echo "rolled back to $previous" >&2 \
        || echo "the rollback to $previous failed too" >&2
else
    echo "nothing ran before it, so there is nothing to roll back to" >&2
fi
exit 1
