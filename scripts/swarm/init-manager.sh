#!/bin/sh
set -eu

if [ $# -ne 1 ]; then
    echo "usage: $0 <advertise-ip>" >&2
    exit 2
fi

docker swarm init --advertise-addr "$1"
node=$(docker info --format '{{.Swarm.NodeID}}')
docker node update --label-add imagegen.infra=true "$node" > /dev/null

echo "Labelled this machine imagegen.infra=true. After each app machine joins, label it from here with"
echo "scripts/swarm/label-app-node.sh <node>. To test on this machine alone, label this node too."
