#!/usr/bin/env bash
# Browser end-to-end suite (used by CI and runnable locally).
#   - starts the simulator in production mode (no FreeCAD) on :8000
#   - API hardening probe, HMI click-through, mimic layout, HiDPI charts, 3-D WebGL page
#   - FreeCAD bridge against a mock RPC server (healthy / stalled / restarted / absent)
# Artifacts (screenshots, server logs) go to $OUT (default: e2e-artifacts/).
set -euo pipefail

OUT="${OUT:-e2e-artifacts}"
PY="${PY:-uv run --no-sync python}"
mkdir -p "$OUT/shots"
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

$PY -m cemsim.server --port 8000 >"$OUT/server.log" 2>&1 & pids+=($!)
$PY tests/validation/mock_freecad.py 9901 >"$OUT/mock_freecad.log" 2>&1 & pids+=($!)
$PY -m cemsim.server --port 8010 --freecad 127.0.0.1:9901 >"$OUT/server_fc.log" 2>&1 & pids+=($!)
$PY -m cemsim.server --port 8011 --freecad 127.0.0.1:9909 >"$OUT/server_fc_dead.log" 2>&1 & pids+=($!)
wait_http http://localhost:8000/api/snapshots 60
wait_http http://localhost:8010/api/snapshots 60
wait_http http://localhost:8011/api/snapshots 60

status=0
run() { echo; echo "::group::$1"; shift; if "$@"; then echo "::endgroup::"; else status=1; echo "::endgroup::"; echo "::error::$*"; fi; }

run "API hardening"        $PY tests/validation/api_probe.py localhost:8000
run "HMI click-through"    $PY tests/validation/e2e_browser.py localhost:8000 "$OUT/shots/hmi.png"
run "Mimic layout"         $PY tests/validation/e2e_layout.py localhost:8000 "$OUT/shots/mimic.png"
run "HiDPI charts"         $PY tests/validation/e2e_hidpi.py localhost:8000
run "Live 3-D plant"       $PY tests/validation/e2e_3d.py localhost:8000 "$OUT/shots"
run "FreeCAD bridge (mock)" $PY tests/validation/e2e_freecad.py localhost:8010 9901 localhost:8011

# the API probe must also have left the simulator alive and the HMI must not crash the server
grep -iE "Traceback|Exception in ASGI" "$OUT/server.log" && { echo "::error::server log contains tracebacks"; status=1; }
exit $status
