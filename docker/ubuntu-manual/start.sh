#!/bin/bash
set -euo pipefail
umask 077
mkdir -p "$HOME/.local/state/ubuntu-wechat" "$HOME/Desktop"
LOG_DIR="$HOME/.local/state/ubuntu-wechat"
export XDG_RUNTIME_DIR="/tmp/runtime-$(id -u)"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
Xvfb "$DISPLAY" -screen 0 "$SCREEN" -ac +extension GLX +render -noreset >"$LOG_DIR/xvfb.log" 2>&1 &
for attempt in $(seq 1 40); do
    xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && break
    sleep 0.25
done
xdpyinfo -display "$DISPLAY" >/dev/null
eval "$(dbus-launch --sh-syntax)"
xset s off
xset -dpms || true
xfce4-session >"$LOG_DIR/desktop.log" 2>&1 &
pulseaudio --start --exit-idle-time=-1 || true
fcitx5 -d >"$LOG_DIR/input.log" 2>&1 || true
x11vnc -display "$DISPLAY" -forever -shared -nopw -noxdamage -rfbport 5900 >"$LOG_DIR/vnc.log" 2>&1 &
websockify --web=/usr/share/novnc 6080 localhost:5900 >"$LOG_DIR/novnc.log" 2>&1 &
sleep 2
wechat >"$LOG_DIR/wechat.log" 2>&1 &
# The API now runs in the same Ubuntu container as WeChat.  This gives the
# reader, scheduler, sender and desktop all one account/session boundary.
mkdir -p /home/wechat/accounts /home/wechat/work
backend_supervisor() {
    while true; do
        echo "[$(date -Is)] starting backend" >>"$LOG_DIR/backend.log"
        (cd /app && exec python3 /app/server.py) >>"$LOG_DIR/backend.log" 2>&1 || true
        echo "[$(date -Is)] backend exited; restarting in 2s" >>"$LOG_DIR/backend.log"
        sleep 2
    done
}
backend_supervisor &
echo 'Ubuntu WeChat desktop and management backend ready; bot remains stopped.'
wait
