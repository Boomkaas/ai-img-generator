#!/bin/sh
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
