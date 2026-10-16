#!/bin/sh
# Start as root only to read the mounted secret (a file secret keeps the host file's owner/mode,
# and Compose ignores uid/mode for it), copy it where the service user can read it, then drop
# privileges for good before anything else runs.
set -eu
SRC="${FREECAD_TOKEN_SECRET:-/run/secrets/freecad_token}"
[ -r "$SRC" ] || { echo "token secret $SRC is not readable" >&2; exit 1; }
install -d -m 0700 -o 10001 -g 10001 /run/freecad
install -m 0400 -o 10001 -g 10001 "$SRC" /run/freecad/token
export FREECAD_MCP_TOKEN_FILE=/run/freecad/token
exec setpriv --reuid=10001 --regid=10001 --clear-groups \
  python3 -m duosim.freecad_worker start --port 9875 --remote --allow "127.0.0.1,${ALLOWED_IPS}" \
  --home /home/freecad/worker --token-file /run/freecad/token
