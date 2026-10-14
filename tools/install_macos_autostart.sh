#!/usr/bin/env bash
# Install a boot-disk LaunchAgent that starts Docker Desktop, waits for the
# xinba bind-mount directory, then starts wxbot-ubuntu-manual.
# Also copies the host docker helper onto the boot disk so it no longer depends
# on /Volumes/xinba being mounted at login.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="$HOME/.wxbot"
PLIST="$HOME/Library/LaunchAgents/com.wxbot.ensure-running.plist"
HOST_PLIST="$HOME/Library/LaunchAgents/com.wxbot.host-docker-agent.plist"
PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || { echo "[autostart] ERROR: python3 not found" >&2; exit 1; }

mkdir -p "$DEST_DIR" "$HOME/Library/LaunchAgents"
chmod 700 "$DEST_DIR"

install -m 755 "$ROOT_DIR/tools/ensure_wxbot_running.sh" "$DEST_DIR/ensure_wxbot_running.sh"
install -m 644 "$ROOT_DIR/tools/host_docker_agent.py" "$DEST_DIR/host_docker_agent.py"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.wxbot.ensure-running</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$DEST_DIR/ensure_wxbot_running.sh</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>WXBOT_CONTAINER</key>
    <string>wxbot-ubuntu-manual</string>
    <key>WXBOT_PORT</key>
    <string>5100</string>
    <key>WXBOT_VNC_PORT</key>
    <string>6082</string>
    <key>WXBOT_BIND_DIR</key>
    <string>$ROOT_DIR</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DEST_DIR/ensure_running.log</string>
  <key>StandardErrorPath</key><string>$DEST_DIR/ensure_running.log</string>
</dict>
</plist>
PLIST

# Host helper must not point at /Volumes/xinba; that path is missing until the
# external disk mounts, so KeepAlive would crash-loop at login.
if [[ -f "$HOST_PLIST" ]] || [[ -f "$DEST_DIR/host_docker_agent.py" ]]; then
  cat > "$HOST_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.wxbot.host-docker-agent</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$DEST_DIR/host_docker_agent.py</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/wxbot_host_agent.log</string>
  <key>StandardErrorPath</key><string>/tmp/wxbot_host_agent.log</string>
</dict>
</plist>
PLIST
fi

reload_agent() {
  local label="$1" plist="$2"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl unload "$plist" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null || launchctl load "$plist"
}

reload_agent com.wxbot.ensure-running "$PLIST"
reload_agent com.wxbot.host-docker-agent "$HOST_PLIST"

# Docker Desktop itself is not set to start at login on this Mac.
if osascript -e 'tell application "System Events" to get the path of every login item' 2>/dev/null | grep -Fq "/Applications/Docker.app"; then
  echo "[autostart] Docker.app already in login items"
else
  osascript -e 'tell application "System Events" to make login item at end with properties {path:"/Applications/Docker.app", hidden:true}' >/dev/null
  echo "[autostart] added Docker.app to login items (hidden)"
fi

python3 - <<'PY'
from pathlib import Path
p = Path.home() / "Library/Group Containers/group.com.docker/settings-store.json"
if p.exists():
    import json
    data = json.loads(p.read_text())
    if data.get("AutoStart") is not True:
        data["AutoStart"] = True
        p.write_text(json.dumps(data, indent=2) + "\n")
        print("[autostart] set Docker Desktop AutoStart=true")
    else:
        print("[autostart] Docker Desktop AutoStart already true")
PY

echo "[autostart] LaunchAgent: $PLIST"
echo "[autostart] watchdog: $DEST_DIR/ensure_wxbot_running.sh"
echo "[autostart] log: $DEST_DIR/ensure_running.log"
echo "[autostart] next reboot: Docker starts, xinba mounts, then wxbot-ubuntu-manual is started"
