#!/usr/bin/env bash
# Install the wxbot host console (registry only). This is not the docker agent.
# It writes LaunchAgent com.wxbot.console and never touches
# com.wxbot.host-docker-agent or that agent's ProgramArguments.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || { echo "[wxbot-console] ERROR: python3 not found" >&2; exit 1; }

CONSOLE_DIR="$HOME/.wxbot/console"
mkdir -p "$CONSOLE_DIR"
install -m 644 "$ROOT_DIR/tools/wxbot_console.py" "$CONSOLE_DIR/wxbot_console.py"

PORT="${WXBOT_CONSOLE_PORT:-5090}"

case "$(uname -s)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.wxbot.console.plist"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.wxbot.console</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$CONSOLE_DIR/wxbot_console.py</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>WXBOT_CONSOLE_PORT</key><string>$PORT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$CONSOLE_DIR/console.log</string>
  <key>StandardErrorPath</key><string>$CONSOLE_DIR/console.log</string>
</dict>
</plist>
PLIST
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    sleep 1
    if curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      echo "[wxbot-console] LaunchAgent loaded and healthy on 127.0.0.1:${PORT}"
    else
      echo "[wxbot-console] LaunchAgent loaded; health check pending. Log: $CONSOLE_DIR/console.log"
    fi
    ;;
  *)
    echo "[wxbot-console] Non-macOS host. Run under a supervisor, e.g.:"
    echo "    WXBOT_CONSOLE_PORT=$PORT nohup $PY $ROOT_DIR/tools/wxbot_console.py &"
    ;;
esac

echo "[wxbot-console] done. This did not change com.wxbot.host-docker-agent."
