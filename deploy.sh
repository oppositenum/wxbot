#!/usr/bin/env bash
# One command: validate Docker, prepare local settings, pull/build, start, wait.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
IMAGE_OVERRIDE="${1:-}"

if (( $# > 1 )); then
  echo "usage: ./deploy.sh [registry/image:tag]" >&2
  exit 2
fi
if [[ -n "$IMAGE_OVERRIDE" && ! "$IMAGE_OVERRIDE" =~ ^[A-Za-z0-9._/:@-]+$ ]]; then
  echo "[deploy] ERROR: invalid Docker image reference" >&2
  exit 2
fi

die() { echo "[deploy] ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }

need docker
need curl
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"
docker info >/dev/null 2>&1 || die "Docker is not running"

rand_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 16
  else
    od -An -N16 -tx1 /dev/urandom | tr -d ' \n'
  fi
}

ensure_env_key() {
  local key="$1" value="$2"
  if grep -q "^${key}=" .env 2>/dev/null; then
    local current
    current="$(sed -n "s/^${key}=//p" .env | head -1)"
    if [[ -n "$current" && "$current" != "GENERATE_ON_FIRST_DEPLOY" ]]; then
      return
    fi
    sed -i.bak "s|^${key}=.*|${key}=${value}|" .env
    rm -f .env.bak
  else
    printf '\n%s=%s\n' "$key" "$value" >> .env
  fi
}

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "[deploy] created .env from .env.example"
fi
ensure_env_key VNC_PASSWORD "$(rand_hex)"
ensure_env_key WXBOT_UI_USER admin
ensure_env_key WXBOT_UI_PASSWORD "$(rand_hex)"
ensure_env_key WXBOT_SECRET "$(rand_hex)"
ensure_env_key WXBOT_UI_AUTH 1

if [[ -n "$IMAGE_OVERRIDE" ]]; then
  sed -i.bak "s|^WXBOT_IMAGE=.*|WXBOT_IMAGE=${IMAGE_OVERRIDE}|" .env
  rm -f .env.bak
  echo "[deploy] saved image: $IMAGE_OVERRIDE"
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

: "${WXBOT_IMAGE:=wxbot-wechat:ubuntu-24.04}"
if [[ -z "${VNC_PASSWORD:-}" || "$VNC_PASSWORD" == "GENERATE_ON_FIRST_DEPLOY" ]]; then
  die "set a non-empty VNC_PASSWORD in .env"
fi
if [[ -z "${WXBOT_UI_USER:-}" || -z "${WXBOT_UI_PASSWORD:-}" || "$WXBOT_UI_PASSWORD" == "GENERATE_ON_FIRST_DEPLOY" ]]; then
  die "set WXBOT_UI_USER and WXBOT_UI_PASSWORD in .env"
fi
if [[ -z "${WXBOT_SECRET:-}" || "$WXBOT_SECRET" == "GENERATE_ON_FIRST_DEPLOY" ]]; then
  die "set WXBOT_SECRET in .env"
fi

is_ubuntu_image() {
  [[ "$(docker image inspect "$WXBOT_IMAGE" \
    --format '{{index .Config.Labels "com.oppositenum.wxbot.runtime"}}' \
    2>/dev/null || true)" == "ubuntu-24.04" ]]
}

echo "[deploy] preparing Ubuntu image: $WXBOT_IMAGE"
docker pull "$WXBOT_IMAGE" >/dev/null 2>&1 || true
if ! is_ubuntu_image; then
  echo "[deploy] registry image is absent or not the Ubuntu build; building locally"
  docker compose build wxbot
fi
is_ubuntu_image || die "image validation failed: expected Ubuntu 24.04 wxbot image"

# Ensure the host-side docker helper (for multi-account start/stop) is installed
# and its token is in .env before Compose reads the environment.
if [[ -x tools/install_host_agent.sh ]]; then
  echo "[deploy] setting up multi-account host helper"
  ./tools/install_host_agent.sh || echo "[deploy] WARN: host helper setup skipped (multi-account start/stop may be unavailable)"
  set -a; source .env; set +a
fi

docker compose up -d --no-build --remove-orphans

echo "[deploy] waiting for management backend"
ready=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 2 http://127.0.0.1:5100/login >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" != "1" ]]; then
  docker compose ps
  docker compose logs --tail 80 wxbot
  die "backend did not become ready within 120 seconds"
fi

echo "[deploy] ready: http://127.0.0.1:5100"
echo "[deploy] management login is WXBOT_UI_USER / WXBOT_UI_PASSWORD in $ROOT_DIR/.env"
echo "[deploy] first WeChat scan: http://127.0.0.1:6080/vnc.html"
echo "[deploy] VNC password is VNC_PASSWORD in $ROOT_DIR/.env"
