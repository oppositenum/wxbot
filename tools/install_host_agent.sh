#!/usr/bin/env bash
# Install the wxbot host-side docker helper so multi-account start/stop/restart
# works from the in-container management UI (which has no docker socket).
#
# What it does (idempotent):
#   1. Generates a shared token (once) and stores it in ~/.wxbot/host_agent.token
#   2. Writes the same token into .env as WXBOT_HOST_AGENT_TOKEN (compose passes
#      it into the container; container also reads the file for ad-hoc runs)
#   3. On macOS: installs & loads a LaunchAgent so the helper auto-starts on boot
#      On Linux: prints the command to run it under systemd/nohup
#
# Re-run any time; it reuses the existing token.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || { echo "[host-agent] ERROR: python3 not found" >&2; exit 1; }

TOKEN_DIR="$HOME/.wxbot"
TOKEN_FILE="$TOKEN_DIR/host_agent.token"
mkdir -p "$TOKEN_DIR"

# 1. token (reuse if present)
if [[ -s "$TOKEN_FILE" ]]; then
  TOKEN="$(tr -d ' \n' < "$TOKEN_FILE")"
else
  TOKEN="$("$PY" -c 'import secrets;print(secrets.token_urlsafe(24))')"
  printf '%s' "$TOKEN" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
fi
echo "[host-agent] token file: $TOKEN_FILE"

# 2. mirror into .env for docker-compose
if [[ -f .env ]]; then
  if grep -q '^WXBOT_HOST_AGENT_TOKEN=' .env; then
    sed -i.bak "s|^WXBOT_HOST_AGENT_TOKEN=.*|WXBOT_HOST_AGENT_TOKEN=${TOKEN}|" .env
  else
    printf '\nWXBOT_HOST_AGENT_TOKEN=%s\n' "$TOKEN" >> .env
  fi
  grep -q '^WXBOT_HOST_AGENT_URL=' .env || printf 'WXBOT_HOST_AGENT_URL=http://host.docker.internal:5199\n' >> .env
  grep -q '^WXBOT_SELF_CONTAINER=' .env || printf 'WXBOT_SELF_CONTAINER=wxbot\n' >> .env
  rm -f .env.bak
  echo "[host-agent] wrote WXBOT_HOST_AGENT_TOKEN into .env"
else
  echo "[host-agent] NOTE: .env not found; run ./deploy.sh first, then re-run this."
fi

# 3. service
case "$(uname -s)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.wxbot.host-docker-agent.plist"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.wxbot.host-docker-agent</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$ROOT_DIR/tools/host_docker_agent.py</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/wxbot_host_agent.log</string>
  <key>StandardErrorPath</key><string>/tmp/wxbot_host_agent.log</string>
</dict>
</plist>
PLIST
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    sleep 1
    if curl -fsS --max-time 3 http://127.0.0.1:5199/health >/dev/null 2>&1; then
      echo "[host-agent] LaunchAgent loaded and healthy on :5199"
    else
      echo "[host-agent] LaunchAgent loaded; health check pending. Log: /tmp/wxbot_host_agent.log"
    fi
    ;;
  *)
    echo "[host-agent] Non-macOS host. Run under a supervisor, e.g.:"
    echo "    WXBOT_HOST_AGENT_TOKEN_FILE=$TOKEN_FILE nohup $PY $ROOT_DIR/tools/host_docker_agent.py &"
    ;;
esac

echo "[host-agent] done. Recreate the wxbot container (./deploy.sh) so it picks up the token env."
