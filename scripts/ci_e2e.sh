#!/usr/bin/env bash
# Browser end-to-end suite (used by CI and runnable locally).
#   - starts the simulator in production mode (no FreeCAD) on :8000
#   - API hardening probe, HMI click-through, mimic layout, HiDPI charts, 3-D WebGL page
#   - FreeCAD bridge against a mock RPC server (healthy / stalled / restarted / absent)
# Artifacts (screenshots, server logs) go to $OUT (default: e2e-artifacts/).
set -euo pipefail

OUT="${OUT:-e2e-artifacts}"
PY="${PY:-uv run --no-sync python}"
# ports can be moved when 8000 etc. belong to something else (e.g. a running training server)
P_APP="${E2E_PORT_APP:-8000}"; P_FC="${E2E_PORT_FC:-8010}"; P_DEAD="${E2E_PORT_DEAD:-8011}"; P_MOCK="${E2E_PORT_MOCK:-9901}"
mkdir -p "$OUT/shots"

# Never test a server we did not start: these suites click buttons, load filesets and inject
# disturbances, which would hit whatever already answers on the port.
for p in "$P_APP" "$P_FC" "$P_DEAD" "$P_MOCK"; do
  if $PY - "$p" <<'PYEOF'
import socket, sys
s = socket.socket(); s.settimeout(0.5)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PYEOF
  then
    echo "port $p is already in use: refusing to run the e2e suite against a server it did not start" >&2
    echo "(set E2E_PORT_APP / E2E_PORT_FC / E2E_PORT_DEAD / E2E_PORT_MOCK to free ports)" >&2
    exit 2
  fi
done
pids=()
cleanup() { for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

wait_http() {  # url, timeout s
  for _ in $(seq 1 "$2"); do
    if curl -fsS "$1" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "timeout waiting for $1" >&2; return 1
}

$PY -m cemsim.server --port "$P_APP" >"$OUT/server.log" 2>&1 & pids+=($!)
$PY tests/validation/mock_freecad.py "$P_MOCK" >"$OUT/mock_freecad.log" 2>&1 & pids+=($!)
$PY -m cemsim.server --port "$P_FC" --freecad "127.0.0.1:$P_MOCK" >"$OUT/server_fc.log" 2>&1 & pids+=($!)
$PY -m cemsim.server --port "$P_DEAD" --freecad 127.0.0.1:9909 >"$OUT/server_fc_dead.log" 2>&1 & pids+=($!)
wait_http "http://localhost:$P_APP/api/snapshots" 60
wait_http "http://localhost:$P_FC/api/snapshots" 60
wait_http "http://localhost:$P_DEAD/api/snapshots" 60

status=0
run() { echo; echo "::group::$1"; shift; if "$@"; then echo "::endgroup::"; else status=1; echo "::endgroup::"; echo "::error::$*"; fi; }

run "API hardening"        $PY tests/validation/api_probe.py localhost:$P_APP
run "HMI click-through"    $PY tests/validation/e2e_browser.py localhost:$P_APP "$OUT/shots/hmi.png"
run "Mimic layout"         $PY tests/validation/e2e_layout.py localhost:$P_APP "$OUT/shots/mimic.png"
run "HiDPI charts"         $PY tests/validation/e2e_hidpi.py localhost:$P_APP
run "Live 3-D plant"       $PY tests/validation/e2e_3d.py localhost:$P_APP "$OUT/shots"
run "FreeCAD bridge (mock)" $PY tests/validation/e2e_freecad.py localhost:$P_FC $P_MOCK localhost:$P_DEAD

# the API probe must also have left the simulator alive and the HMI must not crash the server
grep -iE "Traceback|Exception in ASGI" "$OUT/server.log" && { echo "::error::server log contains tracebacks"; status=1; }
exit $status
