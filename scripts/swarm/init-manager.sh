#!/bin/sh
# Run on machine A: creates the swarm with this machine as the manager, marks it for the infrastructure
# services (Nginx, Redis, Postgres, MinIO, controller) and prints the command the app machines join with.
#
#   scripts/swarm/init-manager.sh <private IP of this machine>
set -eu

if [ $# -ne 1 ]; then
    echo "usage: $0 <advertise-ip>" >&2
    exit 2
fi

# Prints the join command for the app machines; docker swarm join-token worker shows it again later.
docker swarm init --advertise-addr "$1"
node=$(docker info --format '{{.Swarm.NodeID}}')
docker node update --label-add imagegen.infra=true "$node" > /dev/null

echo "Labelled this machine imagegen.infra=true. After each app machine joins, label it from here with"
echo "scripts/swarm/label-app-node.sh <node>. To test on this machine alone, label this node too."
