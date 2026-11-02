#!/bin/sh
set -eu

if [ $# -ne 1 ]; then
    echo "usage: $0 <node>" >&2
    exit 2
fi

docker node update --label-add imagegen.app=true "$1"
docker node inspect --format '{{.Description.Hostname}}: {{.Spec.Labels}}' "$1"
