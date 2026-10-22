#!/bin/sh
# Run on the manager: lets Swarm place API servers and workers on a machine.
#
#   scripts/swarm/label-app-node.sh <node>     (a hostname or ID from docker node ls)
set -eu

if [ $# -ne 1 ]; then
    echo "usage: $0 <node>" >&2
    exit 2
fi

docker node update --label-add imagegen.app=true "$1"
docker node inspect --format '{{.Description.Hostname}}: {{.Spec.Labels}}' "$1"
