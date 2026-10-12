#!/usr/bin/env bash
# CemSim server-side deploy.  Run by the CD workflow over SSH (as the key's forced command) or by hand:
#
#     scripts/deploy.sh deploy <40-hex-commit>   move to that commit, verify, restart, health-check
#     scripts/deploy.sh rollback                 return to the previously deployed commit
#     scripts/deploy.sh status                   what is deployed, and the recent history
#
# Safety properties (each one is exercised by tests/test_deploy_script.py):
#   * only a full commit hash is accepted - nothing the caller types reaches a shell
#   * only commits reachable from origin/main can be deployed (an unreviewed branch cannot)
#   * a dirty working tree aborts the deploy instead of being overwritten
#   * one deploy at a time (flock)
#   * dependencies and a fast test gate must pass BEFORE any service is restarted
#   * if the health check fails after the restart, the previous commit is restored automatically
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

STATE="${CEMSIM_DEPLOY_STATE:-$ROOT/.cemsim/deploy}"
BRANCH="${CEMSIM_DEPLOY_BRANCH:-main}"
REMOTE="${CEMSIM_DEPLOY_REMOTE:-origin}"
# restart order matters: the FreeCAD worker first, then the app that talks to it
UNITS="${CEMSIM_UNITS:-cemsim-freecad.service cemsim.service}"
SYNC_CMD="${CEMSIM_DEPLOY_SYNC_CMD:-uv sync --frozen --extra ai}"
TEST_CMD="${CEMSIM_DEPLOY_TEST_CMD:-.venv/bin/python -m pytest -q -x -p no:cacheprovider tests/test_design_spec.py tests/test_design_llm.py tests/test_api_design.py tests/test_platform.py tests/test_server.py}"
HEALTH_URLS="${CEMSIM_HEALTH_URLS:-http://127.0.0.1:8000/api/snapshots}"
HEALTH_TIMEOUT="${CEMSIM_HEALTH_TIMEOUT:-90}"

log() { printf '%s deploy: %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit "${2:-1}"; }

as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -n "$@"; fi; }

# the forced command arrives in SSH_ORIGINAL_COMMAND; by hand it is argv
request="${SSH_ORIGINAL_COMMAND:-$*}"
read -r verb arg extra <<<"$request" || true
[ -z "${extra:-}" ] || die "unexpected arguments"

mkdir -p "$STATE"

unit_exists() { as_root systemctl cat "$1" >/dev/null 2>&1; }

restart_units() {
  local u
  for u in $UNITS; do
    if unit_exists "$u"; then
      log "restarting $u"
      as_root systemctl restart "$u"
    else
      log "unit $u is not installed here: skipped"
    fi
  done
}

healthy() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) url ok
  for url in $HEALTH_URLS; do
    ok=0
    while [ "$SECONDS" -lt "$deadline" ]; do
      if curl -fsS -m 5 -o /dev/null "$url"; then ok=1; break; fi
      sleep 2
    done
    [ "$ok" -eq 1 ] || { log "health check failed: $url"; return 1; }
  done
}

current() { git rev-parse HEAD; }

record() { printf '%s %s -> %s %s\n' "$(date -u +%FT%TZ)" "$1" "$2" "$3" >>"$STATE/history.log"; }

go_to() {  # check out $1, install dependencies
  git checkout --quiet --detach "$1"
  bash -c "$SYNC_CMD"
}

case "${verb:-}" in
  status)
    log "deployed: $(current)"
    [ -f "$STATE/previous" ] && log "previous: $(cat "$STATE/previous")"
    tail -n 8 "$STATE/history.log" 2>/dev/null || true
    exit 0
    ;;
  deploy | rollback) ;;
  *) die "usage: deploy.sh deploy <40-hex-sha> | rollback | status" 64 ;;
esac

exec 9>"$STATE/lock"
flock -n 9 || die "another deploy is running" 3

if ! git diff --quiet || ! git diff --cached --quiet; then
  die "the working tree has uncommitted changes; refusing to overwrite them" 4
fi

before="$(current)"

if [ "$verb" = "rollback" ]; then
  [ -s "$STATE/previous" ] || die "no previous deployment recorded" 5
  target="$(cat "$STATE/previous")"
else
  target="${arg:-}"
fi
[[ "$target" =~ ^[0-9a-f]{40}$ ]] || die "a full 40-character commit hash is required" 64

log "fetching $REMOTE"
git fetch --quiet --prune "$REMOTE"
git cat-file -e "$target^{commit}" 2>/dev/null || die "commit $target is not in the repository" 6
git merge-base --is-ancestor "$target" "$REMOTE/$BRANCH" || die "commit $target is not on $REMOTE/$BRANCH; refusing to deploy it" 7

if [ "$target" = "$before" ] && [ "$verb" = "deploy" ]; then
  log "already at $target: re-verifying and restarting"
fi

rollback_to_previous() {
  log "restoring $before"
  go_to "$before"
  restart_units
  if healthy; then
    record "$target" "$before" "ROLLED-BACK"
    die "deploy of $target failed; rolled back to $before" 1
  fi
  record "$target" "$before" "ROLLBACK-FAILED"
  die "deploy of $target failed AND the rollback to $before is unhealthy: manual action needed" 2
}

log "deploying $target (currently $before)"
go_to "$target" || { log "dependency install failed"; git checkout --quiet --detach "$before"; die "install failed; nothing was restarted" 8; }

log "pre-restart test gate"
if ! bash -c "$TEST_CMD"; then
  git checkout --quiet --detach "$before"
  bash -c "$SYNC_CMD" || true
  record "$target" "$before" "TESTS-FAILED"
  die "the test gate failed; nothing was restarted, still on $before" 9
fi

restart_units
if healthy; then
  if [ "$target" != "$before" ]; then printf '%s\n' "$before" >"$STATE/previous"; fi
  printf '%s\n' "$target" >"$STATE/current"
  record "$before" "$target" "OK"
  log "deployed $target"
else
  rollback_to_previous
fi
