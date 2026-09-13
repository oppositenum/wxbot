#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

STATE_DIR="$HOME/.local/state/wxbot"
RUNTIME_DIR="/tmp/runtime-$(id -u)"
mkdir -p "$STATE_DIR" "$RUNTIME_DIR" "$HOME/Desktop" /app/accounts /app/work
chmod 700 "$RUNTIME_DIR"
export XDG_RUNTIME_DIR="$RUNTIME_DIR"

Xvfb "$DISPLAY" -screen 0 "$SCREEN" -ac +extension GLX +render -noreset \
  >"$STATE_DIR/xvfb.log" 2>&1 &
for _ in $(seq 1 40); do
  if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
xdpyinfo -display "$DISPLAY" >/dev/null

eval "$(dbus-launch --sh-syntax)"
xset s off
xset -dpms || true
xfce4-session >"$STATE_DIR/desktop.log" 2>&1 &
pulseaudio --start --exit-idle-time=-1 || true
fcitx5 -d >"$STATE_DIR/input.log" 2>&1 || true

VNC_ARGS=(-display "$DISPLAY" -forever -shared -noxdamage -rfbport 5900)
if [[ -n "${VNC_PASSWORD:-}" ]]; then
  x11vnc -storepasswd "$VNC_PASSWORD" "$STATE_DIR/vnc.pass" >/dev/null
  VNC_ARGS+=(-rfbauth "$STATE_DIR/vnc.pass")
else
  VNC_ARGS+=(-nopw)
  echo "[wxbot] WARNING: VNC_PASSWORD is empty"
fi
x11vnc "${VNC_ARGS[@]}" >"$STATE_DIR/vnc.log" 2>&1 &
websockify --web=/usr/share/novnc 6080 localhost:5900 \
  >"$STATE_DIR/novnc.log" 2>&1 &

sleep 2
wechat >"$STATE_DIR/wechat.log" 2>&1 &

if [[ "${WXBOT_MEDIA_CAPTURE:-1}" == "1" ]] \
    && [[ -f /app/docker/frida_capture_supervisor.py ]]; then
  (sleep 15; python3 /app/docker/frida_capture_supervisor.py \
    >"$STATE_DIR/frida-capture.log" 2>&1) &
fi

backend_supervisor() {
  while true; do
    echo "[$(date -Is)] starting backend" >>"$STATE_DIR/backend.log"
    (cd /app && python3 /app/server.py) >>"$STATE_DIR/backend.log" 2>&1 || true
    echo "[$(date -Is)] backend exited; restarting in 2s" >>"$STATE_DIR/backend.log"
    sleep 2
  done
}
backend_supervisor &

echo "[wxbot] Ubuntu desktop, WeChat and backend started"
echo "[wxbot] noVNC :6080, management backend :5100"
wait
