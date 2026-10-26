#!/bin/sh
# The controller scales the workers through the host's Docker socket, whose group id differs from host to
# host. Start as root only long enough to read that id, then run the controller as the unprivileged user
# with the socket's group added.
set -eu

socket=/var/run/docker.sock
if [ -S "$socket" ]; then
    exec setpriv --reuid=app --regid=app --groups="$(stat -c %g "$socket")" -- "$@"
fi
exec setpriv --reuid=app --regid=app --clear-groups -- "$@"
