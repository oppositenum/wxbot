#!/usr/bin/env bash
# Keep the existing wxbot Docker Desktop container reachable after a Mac reboot.
# Lives on the boot disk (~/.wxbot) so it can start before /Volumes/xinba mounts.
set -u

CONTAINER="${WXBOT_CONTAINER:-wxbot-ubuntu-manual}"
PORT="${WXBOT_PORT:-5100}"
VNC_PORT="${WXBOT_VNC_PORT:-6082}"
BIND_DIR="${WXBOT_BIND_DIR:-/Volumes/xinba/10_Projects/Active/oppositenum/wxbot}"
LOG="${WXBOT_ENSURE_LOG:-$HOME/.wxbot/ensure_running.log}"
DOCKER_APP="${DOCKER_APP:-/Applications/Docker.app}"
POLL="${WXBOT_ENSURE_POLL:-20}"

mkdir -p "$(dirname "$LOG")"
PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

log() { printf '%s %s\n' "$(date -Iseconds)" "$*" >>"$LOG"; }

docker_ready() { docker info >/dev/null 2>&1; }

open_docker() {
  if [[ -d "$DOCKER_APP" ]]; then
    open -ga "$DOCKER_APP" >/dev/null 2>&1 || true
  fi
}

bind_ready() {
  [[ -d "$BIND_DIR/accounts" && -d "$BIND_DIR/output/ubuntu-manual-images" ]]
}

backend_ready() {
  curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/login" >/dev/null 2>&1
}

vnc_ready() {
  curl -fsS --max-time 2 "http://127.0.0.1:${VNC_PORT}/vnc.html" >/dev/null 2>&1
}

ensure_once() {
  if [[ -f "$HOME/.wxbot/skip-autostart" ]]; then
    return 0
  fi
  if ! docker_ready; then
    open_docker
    return 1
  fi
  if ! bind_ready; then
    return 1
  fi
  if ! docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    log "container missing: $CONTAINER"
    return 1
  fi
  local state
  state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo missing)"
  if [[ "$state" != "running" ]]; then
    log "starting $CONTAINER (was $state)"
    docker start "$CONTAINER" >>"$LOG" 2>&1 || return 1
  fi
  if backend_ready && vnc_ready; then
    return 0
  fi
  return 1
}

log "ensure loop started container=$CONTAINER bind=$BIND_DIR"
while true; do
  if ensure_once; then
    :
  fi
  sleep "$POLL"
done
