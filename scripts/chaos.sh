#!/bin/sh
# Run on the Swarm manager against a deployed stack: kills one worker container, then one API container, then
# the controller, each with SIGKILL and with jobs waiting, and after each kill checks that
#   - every job that was waiting at the kill still finishes as done,
#   - the killed service is running at its full replica count again within 60 s,
#   - none of the round's jobs is left running.
# Prints PASS or FAIL per check and a summary; exits 1 if anything failed.
#
#   scripts/chaos.sh [https://<domain>]        (default https://localhost)
#
# JOBS (default 4) users each submit one 256 px job per round; JOB_TIMEOUT_S (default 1800) is how long a
# round's jobs may take to finish. Containers on other machines are killed over ssh to the node's hostname.
# Needs curl and jq. It signs up throwaway users, so point it at a test deployment.
set -eu

site=${1:-https://localhost}
stack=${STACK:-imagegen}
jobs=${JOBS:-4}
job_timeout=${JOB_TIMEOUT_S:-1800}
back_within=60
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
failures=0
summary=""

for tool in curl jq docker; do
    command -v "$tool" > /dev/null || { echo "$tool is required" >&2; exit 2; }
done

check() {  # check <ok: 0 or 1> <message>
    if [ "$1" -eq 0 ]; then line="PASS $2"; else line="FAIL $2"; failures=$((failures + 1)); fi
    echo "$line"
    summary="$summary
$line"
}

psql_query() {
    container=$(docker ps -q -f "label=com.docker.swarm.service.name=${stack}_postgres" | head -n 1)
    docker exec "$container" psql -U imagegen -d imagegen -tA -c "$1"
}

uuid() { cat /proc/sys/kernel/random/uuid; }

api() {  # api <user> <method> <path> [json body]
    if [ $# -ge 4 ]; then
        curl -sk -b "$work/$1" -c "$work/$1" -X "$2" -H 'Content-Type: application/json' \
            -H "Idempotency-Key: $(uuid)" -d "$4" "$site$3"
    else
        curl -sk -b "$work/$1" -c "$work/$1" -X "$2" "$site$3"
    fi
}

sign_up() {  # Nginx allows ten sign-ups a minute from one address, plus a burst of five
    while :; do
        code=$(curl -sk -o /dev/null -w '%{http_code}' -c "$work/$1" -H 'Content-Type: application/json' \
            -d "{\"email\":\"chaos-$(uuid)@example.com\",\"password\":\"chaos-test-password\"}" \
            "$site/api/auth/register")
        [ "$code" = 201 ] && return
        [ "$code" = 429 ] || { echo "sign-up answered $code" >&2; exit 1; }
        sleep 7
    done
}

running_tasks() {  # the service's tasks that should be running: "<task id> <current state>"
    docker service ps "${stack}_$1" --filter desired-state=running --format '{{.ID}} {{.CurrentState}}'
}

kill_one() {  # SIGKILL one running container of the service, wherever it runs; prints the task id
    task=$(running_tasks "$1" | awk '$2 == "Running" {print $1; exit}')
    [ -n "$task" ] || { echo "no running task of ${stack}_$1" >&2; exit 1; }
    container=$(docker inspect -f '{{.Status.ContainerStatus.ContainerID}}' "$task")
    node=$(docker node inspect -f '{{.Description.Hostname}}' "$(docker inspect -f '{{.NodeID}}' "$task")")
    if [ "$node" = "$(docker info -f '{{.Name}}')" ]; then
        docker kill "$container" > /dev/null
    else
        ssh "$node" docker kill "$container" > /dev/null
    fi
    echo "$task"
}

back_in() {  # seconds until the service runs its full replica count again without the killed task, or -1
    started=$(date +%s)
    while [ $(($(date +%s) - started)) -le "$back_within" ]; do
        desired=$(docker service inspect -f '{{.Spec.Mode.Replicated.Replicas}}' "${stack}_$1")
        up=$(running_tasks "$1" | awk -v gone="$2" '$1 != gone && $2 == "Running"' | wc -l)
        if [ "$up" -ge "$desired" ]; then
            echo $(($(date +%s) - started))
            return
        fi
        sleep 2
    done
    echo -1
}

ids_sql() { echo "$1" | sed "s/^/'/; s/\$/'/" | paste -sd, -; }

i=1
while [ "$i" -le "$jobs" ]; do sign_up "user$i"; i=$((i + 1)); done

for service in worker api controller; do
    echo "--- killing one ${stack}_$service container"
    ids=""
    i=1
    while [ "$i" -le "$jobs" ]; do
        id=$(api "user$i" POST /api/tasks '{"prompt":"chaos test","size":256}' | jq -r .task_id)
        case $id in null | *[!0-9a-f-]*) echo "submit failed for user$i: $id" >&2; exit 1 ;; esac
        ids="$ids${ids:+
}$id"
        i=$((i + 1))
    done
    sleep 3  # let the first jobs start
    in_list=$(ids_sql "$ids")
    waiting=$(psql_query "SELECT id FROM tasks WHERE id IN ($in_list) AND status = 'queued'")
    killed=$(kill_one "$service")
    seconds=$(back_in "$service" "$killed")
    [ "$seconds" -ge 0 ] && ok=0 || ok=1
    check "$ok" "$service: back at full strength within ${back_within} s (took ${seconds} s)"

    started=$(date +%s)
    while [ "$(psql_query "SELECT count(*) FROM tasks WHERE id IN ($in_list) AND status IN ('queued', 'running')")" -gt 0 ] \
        && [ $(($(date +%s) - started)) -lt "$job_timeout" ]; do
        sleep 5
    done
    left=$(psql_query "SELECT count(*) FROM tasks WHERE id IN ($in_list) AND status IN ('queued', 'running')")
    [ "$left" -eq 0 ] && ok=0 || ok=1
    check "$ok" "$service: none of the round's jobs left queued or running ($left left)"

    if [ -n "$waiting" ]; then
        total=$(echo "$waiting" | wc -l)
        done_count=$(psql_query "SELECT count(*) FROM tasks WHERE id IN ($(ids_sql "$waiting")) AND status = 'done'")
    else
        total=0
        done_count=0
    fi
    [ "$done_count" -eq "$total" ] && ok=0 || ok=1
    check "$ok" "$service: every job waiting at the kill finished as done ($done_count/$total)"
    psql_query "SELECT status, count(*) FROM tasks WHERE id IN ($in_list) GROUP BY status" | sed 's/^/    /'
    sleep 10
done

echo "--- summary$summary"
if [ "$failures" -gt 0 ]; then
    echo "$failures check(s) failed"
    exit 1
fi
echo "all checks passed"
