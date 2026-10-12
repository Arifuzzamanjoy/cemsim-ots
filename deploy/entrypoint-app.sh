#!/bin/sh
# Same pattern as the worker: read the shared secret as root, hand it to the unprivileged service
# through its environment, and drop privileges before the server starts.
set -eu
SRC="${FREECAD_TOKEN_SECRET:-/run/secrets/freecad_token}"
if [ -r "$SRC" ]; then
  FREECAD_MCP_TOKEN="$(cat "$SRC")"
  export FREECAD_MCP_TOKEN
fi
exec setpriv --reuid=10002 --regid=10002 --init-groups "$@"
