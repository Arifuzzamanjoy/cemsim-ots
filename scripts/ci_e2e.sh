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
# Each sub-suite's output is kept in $OUT/<name>.log (uploaded as a CI artifact), and every failed check is
# re-emitted as a GitHub annotation so the reason is visible on the run page without opening logs.
run() {
  local name="$1" log; shift
  log="$OUT/$(printf '%s' "$name" | tr -c 'A-Za-z0-9' '_').log"
  echo; echo "::group::$name"
  if "$@" 2>&1 | tee "$log"; then
    echo "::endgroup::"
    return 0
  fi
  echo "::endgroup::"
  grep -E '^FAIL|Traceback|Error' "$log" | head -8 | while IFS= read -r line; do
    echo "::error title=$name::${line}"
  done
  return 1
}
# The FreeCAD-bridge and 3-D suites are timing based (back-offs, 1 Hz pushes, software-GL frame rates) and run
# on a shared runner: allow one retry, and say so loudly so that flakiness stays visible instead of hidden.
run_once_retry() {
  local name="$1"; shift
  if run "$name" "$@"; then return 0; fi
  echo "::warning title=$name::first attempt failed; retrying once (timing-sensitive suite)"
  run "$name (retry)" "$@"
}
step() { if ! "$@"; then status=1; fi; }

step run "API hardening"        $PY tests/validation/api_probe.py localhost:$P_APP
step run "HMI click-through"    $PY tests/validation/e2e_browser.py localhost:$P_APP "$OUT/shots/hmi.png"
step run "Mimic layout"         $PY tests/validation/e2e_layout.py localhost:$P_APP "$OUT/shots/mimic.png"
step run "HiDPI charts"         $PY tests/validation/e2e_hidpi.py localhost:$P_APP
step run_once_retry "Live 3-D plant" $PY tests/validation/e2e_3d.py localhost:$P_APP "$OUT/shots"
step run_once_retry "FreeCAD bridge (mock)" $PY tests/validation/e2e_freecad.py localhost:$P_FC $P_MOCK localhost:$P_DEAD

# the API probe must also have left the simulator alive and the HMI must not crash the server
grep -iE "Traceback|Exception in ASGI" "$OUT/server.log" && { echo "::error::server log contains tracebacks"; status=1; }
exit $status
