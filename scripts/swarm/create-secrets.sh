#!/bin/sh
# Run on the manager once, before the first deploy: stores the TLS certificate and key and every password as
# Swarm secrets, and Nginx's list of API servers as a Swarm config.
#
#   scripts/swarm/create-secrets.sh <fullchain.pem> <privkey.pem> <upstreams.inc>
#
# The passwords are read from infra/.env.prod (ENV_FILE overrides the path). The upstreams file has one
# "server <ip>:8000 ...;" line per app machine; see infra/nginx/upstreams.example.inc. The config is named
# after NGINX_UPSTREAMS_CONFIG (default nginx_upstreams).
#
# Secrets can't be changed in place. To change a password: docker stack rm imagegen, docker secret rm the
# imagegen_* secrets, run this again with the new values, and deploy. Postgres keeps the password it was
# initialised with, so change it there first (ALTER USER imagegen PASSWORD ...).
set -eu

if [ $# -ne 3 ]; then
    echo "usage: $0 <cert.pem> <key.pem> <upstreams.inc>" >&2
    exit 2
fi

env_file=${ENV_FILE:-$(dirname "$0")/../../infra/.env.prod}
if [ ! -f "$env_file" ]; then
    echo "$env_file not found; copy infra/.env.prod.example and fill it in" >&2
    exit 1
fi
set -a
. "$env_file"
set +a
for name in REDIS_PASSWORD PG_PASSWORD MINIO_PASSWORD JWT_SECRET; do
    eval "value=\${$name:-}"
    if [ -z "$value" ]; then
        echo "$name is empty in $env_file" >&2
        exit 1
    fi
done

secret() {
    printf '%s' "$2" | docker secret create "$1" - > /dev/null
    echo "created secret $1"
}

docker secret create tls_cert "$1" > /dev/null && echo "created secret tls_cert"
docker secret create tls_key "$2" > /dev/null && echo "created secret tls_key"
docker config create "${NGINX_UPSTREAMS_CONFIG:-nginx_upstreams}" "$3" > /dev/null \
    && echo "created config ${NGINX_UPSTREAMS_CONFIG:-nginx_upstreams}"

secret imagegen_jwt_secret "$JWT_SECRET"
secret imagegen_pg_password "$PG_PASSWORD"
secret imagegen_minio_password "$MINIO_PASSWORD"
secret imagegen_redis_conf "requirepass $REDIS_PASSWORD"
secret imagegen_database_url "postgresql://imagegen:$PG_PASSWORD@postgres/imagegen"
secret imagegen_redis_url "redis://:$REDIS_PASSWORD@redis:6379/0"
secret imagegen_broker_url "redis://:$REDIS_PASSWORD@redis:6379/0"
