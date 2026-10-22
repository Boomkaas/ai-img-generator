#!/bin/sh
# Run on the manager once, before the first deploy: stores the TLS certificate and key as Swarm secrets and
# Nginx's list of API servers as a Swarm config.
#
#   scripts/swarm/create-secrets.sh <fullchain.pem> <privkey.pem> <upstreams.inc>
#
# The upstreams file has one "server <ip>:8000 ...;" line per app machine; see
# infra/nginx/upstreams.example.inc. The config is named after NGINX_UPSTREAMS_CONFIG (default
# nginx_upstreams), which must match infra/.env.prod.
set -eu

if [ $# -ne 3 ]; then
    echo "usage: $0 <cert.pem> <key.pem> <upstreams.inc>" >&2
    exit 2
fi

docker secret create tls_cert "$1"
docker secret create tls_key "$2"
docker config create "${NGINX_UPSTREAMS_CONFIG:-nginx_upstreams}" "$3"
