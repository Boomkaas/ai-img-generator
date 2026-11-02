#!/bin/sh
set -eu

stack=${STACK:-imagegen}
image=${POSTGRES_IMAGE:-postgres:16}
max_age_h=${MAX_AGE_H:-26}
scratch=imagegen-restore-check-$$
failures=0

command -v docker > /dev/null || { echo "docker is required" >&2; exit 2; }

check() {
    if [ "$1" -eq 0 ]; then echo "PASS $2"; else echo "FAIL $2"; failures=$((failures + 1)); fi
}

if [ $# -ge 1 ]; then
    [ -f "$1" ] || { echo "$1 not found" >&2; exit 2; }
    source="$(cd "$(dirname "$1")" && pwd)"
    dump=$(basename "$1")
else
    source=${stack}_backups
    docker volume inspect "$source" > /dev/null 2>&1 \
        || { echo "no volume $source on this machine; run this where the backup service runs" >&2; exit 2; }
    dump=$(docker run --rm -v "$source:/backups:ro" "$image" \
        sh -c 'ls -1t /backups/imagegen-*.dump 2> /dev/null | head -n 1 | xargs -r basename')
    [ -n "$dump" ] || { echo "no dump in $source yet" >&2; exit 1; }
fi
echo "restoring $dump from $source"

trap 'docker rm -f "$scratch" > /dev/null 2>&1 || true' EXIT
docker run -d --name "$scratch" -e POSTGRES_PASSWORD=scratch -e POSTGRES_DB=restore_check \
    -v "$source:/backups:ro" "$image" > /dev/null
scratch_sql() { docker exec "$scratch" psql -U postgres -d restore_check -tA -c "$1"; }

tries=0
until docker exec "$scratch" pg_isready -q -h 127.0.0.1 -U postgres; do
    tries=$((tries + 1))
    [ "$tries" -le 60 ] || { echo "the scratch database did not start" >&2; exit 1; }
    sleep 1
done

written=$(docker exec "$scratch" stat -c %Y "/backups/$dump")
age_h=$((($(date +%s) - written) / 3600))
[ "$age_h" -lt "$max_age_h" ] && ok=0 || ok=1
check "$ok" "the dump is recent ($age_h h old, at most $max_age_h)"

docker exec "$scratch" pg_restore -U postgres -d restore_check --no-owner --no-privileges --exit-on-error \
    "/backups/$dump" && ok=0 || ok=1
check "$ok" "the dump restores without an error"
if [ "$ok" -ne 0 ]; then
    echo "1 or more checks failed"
    exit 1
fi

users=$(scratch_sql "SELECT count(*) FROM users" || echo -1)
tasks=$(scratch_sql "SELECT count(*) FROM tasks" || echo -1)
[ "$users" -ge 1 ] && ok=0 || ok=1
check "$ok" "users came back ($users rows, at least 1)"
[ "$tasks" -ge 0 ] && ok=0 || ok=1
check "$ok" "tasks came back ($tasks rows)"
scratch_sql "SELECT '    ' || status || ': ' || count(*) FROM tasks GROUP BY status ORDER BY status" || true

live=$(docker ps -q -f "label=com.docker.swarm.service.name=${stack}_postgres" | head -n 1)
if [ -n "$live" ]; then
    live_users=$(docker exec "$live" psql -U imagegen -d imagegen -tA -c "SELECT count(*) FROM users")
    [ "$users" -le "$live_users" ] && ok=0 || ok=1
    check "$ok" "no more users than the running database ($users of $live_users)"
else
    echo "SKIP the running database isn't on this machine, so the user count isn't compared"
fi

if [ "$failures" -gt 0 ]; then
    echo "$failures check(s) failed"
    exit 1
fi
echo "the backup restores"
