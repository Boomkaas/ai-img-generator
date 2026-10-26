#!/bin/sh
# Run on the Swarm manager against a deployed stack: submits a burst of jobs from many users, follows the
# worker count on /api/admin/overview, and checks that it
#   - reaches MAX_WORKERS, with workers on at least MIN_MACHINES machines,
#   - never goes above MAX_WORKERS,
#   - falls back to MIN_WORKERS once the jobs are done, with none of them failed.
# Prints the count over time, PASS or FAIL per check, and exits 1 if anything failed.
#
#   ADMIN_EMAIL=... ADMIN_PASSWORD=... scripts/autoscale_check.sh [https://<domain>]
#
# The admin account comes from scripts/create_admin.py. JOBS (default MAX_WORKERS x
# SCALE_UP_WAITING_PER_WORKER + 2) is how many users submit at once; MIN_MACHINES defaults to 2 (use 1 on a
# one-machine swarm); TIMEOUT_S (default 3600) bounds each phase. Needs curl and jq. It signs up throwaway
# users, so point it at a test deployment.
set -eu

site=${1:-https://localhost}
stack=${STACK:-imagegen}
min_machines=${MIN_MACHINES:-2}
timeout=${TIMEOUT_S:-3600}
: "${ADMIN_EMAIL:?set ADMIN_EMAIL and ADMIN_PASSWORD to an admin account}" "${ADMIN_PASSWORD:?}"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
failures=0

for tool in curl jq docker; do
    command -v "$tool" > /dev/null || { echo "$tool is required" >&2; exit 2; }
done

check() {  # check <message> <test ...>
    message=$1
    shift
    if "$@"; then echo "PASS $message"; else echo "FAIL $message"; failures=$((failures + 1)); fi
}

psql_query() {
    container=$(docker ps -q -f "label=com.docker.swarm.service.name=${stack}_postgres" | head -n 1)
    docker exec "$container" psql -U imagegen -d imagegen -tA -c "$1"
}

controller_env() {  # the controller's setting, or the default
    docker service inspect -f '{{json .Spec.TaskTemplate.ContainerSpec.Env}}' "${stack}_controller" \
        | jq -r --arg name "$1" '.[] | select(startswith($name + "=")) | sub("^[^=]*="; "")' | grep . || echo "$2"
}

uuid() { cat /proc/sys/kernel/random/uuid; }

post_json() {  # post_json <cookie jar> <path> <body>; prints the HTTP status, the body goes to <jar>.out
    curl -sk -o "$1.out" -w '%{http_code}' -b "$1" -c "$1" -H 'Content-Type: application/json' \
        -H "Idempotency-Key: $(uuid)" -d "$3" "$site$2"
}

overview() {  # the admin overview; the access token lasts 15 min, so refresh it when it runs out
    for _ in 1 2; do
        code=$(curl -sk -o "$work/overview.json" -w '%{http_code}' -b "$work/admin" -c "$work/admin" \
            "$site/api/admin/overview")
        [ "$code" = 200 ] && { cat "$work/overview.json"; return; }
        post_json "$work/admin" /api/auth/refresh '{}' > /dev/null
    done
    echo "the overview answered $code" >&2
    exit 1
}

credentials=$(jq -nc --arg email "$ADMIN_EMAIL" --arg password "$ADMIN_PASSWORD" \
    '{email: $email, password: $password}')
code=$(post_json "$work/admin" /api/auth/login "$credentials")
[ "$code" = 200 ] || { echo "admin sign-in answered $code: $(cat "$work/admin.out")" >&2; exit 1; }

now=$(overview)
maximum=$(echo "$now" | jq .autoscaler.maximum)
minimum=$(controller_env MIN_WORKERS 1)
per_worker=$(controller_env SCALE_UP_WAITING_PER_WORKER 2)
jobs=${JOBS:-$((maximum * per_worker + 2))}
echo "MIN_WORKERS=$minimum MAX_WORKERS=$maximum; submitting $jobs jobs"

ids=""
i=1
while [ "$i" -le "$jobs" ]; do
    jar="$work/user$i"
    body="{\"email\":\"autoscale-$(uuid)@example.com\",\"password\":\"autoscale-test-password\"}"
    while :; do
        code=$(post_json "$jar" /api/auth/register "$body")
        [ "$code" = 201 ] && break
        [ "$code" = 429 ] || { echo "sign-up answered $code" >&2; exit 1; }
        sleep 7  # Nginx allows ten sign-ups a minute from one address
    done
    code=$(post_json "$jar" /api/tasks '{"prompt":"autoscale test","size":256}')
    [ "$code" = 202 ] || { echo "submit answered $code: $(cat "$jar.out")" >&2; exit 1; }
    id=$(jq -r .task_id "$jar.out")
    case $id in *[!0-9a-f-]*) echo "unexpected task id $id" >&2; exit 1 ;; esac
    ids="$ids${ids:+,}'$id'"
    i=$((i + 1))
done

peak=0
peak_machines=0
highest=0
count=0
sample() {  # one line of the count over time; updates count, peak, peak_machines and highest
    now=$(overview)
    count=$(echo "$now" | jq '.workers | length')
    ready=$(echo "$now" | jq '[.workers[] | select(.state != "loading")] | length')
    machines=$(echo "$now" | jq '[.workers[].machine] | unique | length')
    waiting=$(echo "$now" | jq '[.tasks[] | select(.status == "queued")] | length')
    echo "$(date +%H:%M:%S) workers $count (ready $ready) on $machines machine(s)," \
        "$waiting waiting, desired $(echo "$now" | jq .autoscaler.desired)"
    if [ "$count" -gt "$highest" ]; then highest=$count; fi
    if [ "$ready" -gt "$peak" ] || { [ "$ready" -eq "$peak" ] && [ "$machines" -gt "$peak_machines" ]; }; then
        peak=$ready
        peak_machines=$machines
    fi
}

unfinished() { psql_query "SELECT count(*) FROM tasks WHERE id IN ($ids) AND status IN ('queued', 'running')"; }

started=$(date +%s)
while [ "$(unfinished)" -gt 0 ] && [ $(($(date +%s) - started)) -lt "$timeout" ]; do
    sample
    sleep 5
done
check "every job of the burst has finished" test "$(unfinished)" -eq 0

started=$(date +%s)
while :; do
    sample
    if [ "$count" -le "$minimum" ] || [ $(($(date +%s) - started)) -ge "$timeout" ]; then break; fi
    sleep 5
done

not_done=$(psql_query "SELECT count(*) FROM tasks WHERE id IN ($ids) AND status <> 'done'")
check "reached MAX_WORKERS ready workers ($peak of $maximum)" test "$peak" -eq "$maximum"
check "the workers spread over at least $min_machines machine(s) at the peak ($peak_machines)" \
    test "$peak_machines" -ge "$min_machines"
check "never above MAX_WORKERS (highest $highest)" test "$highest" -le "$maximum"
check "back to MIN_WORKERS ($count of $minimum)" test "$count" -eq "$minimum"
check "no job of the burst failed ($not_done not done)" test "$not_done" -eq 0

if [ "$failures" -gt 0 ]; then
    echo "$failures check(s) failed"
    exit 1
fi
echo "all checks passed"
