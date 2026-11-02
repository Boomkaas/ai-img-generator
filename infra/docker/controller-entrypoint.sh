#!/bin/sh
set -eu

socket=/var/run/docker.sock
if [ -S "$socket" ]; then
    exec setpriv --reuid=app --regid=app --groups="$(stat -c %g "$socket")" -- "$@"
fi
exec setpriv --reuid=app --regid=app --clear-groups -- "$@"
