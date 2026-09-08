#!/bin/bash
# 一体化启动：虚拟X + 窗口管理器 + VNC(可选密码) + noVNC + 微信 + 后端(Flask)
set -e

SCREEN=${SCREEN:-1360x900x24}
export DISPLAY=:0

echo "[start] Xvfb $SCREEN"
Xvfb :0 -screen 0 "$SCREEN" -ac +extension GLX +render -noreset >/var/log/xvfb.log 2>&1 &
sleep 2

# dbus（微信需要）
mkdir -p /run/dbus
dbus-daemon --system --fork 2>/dev/null || true
export $(dbus-launch) 2>/dev/null || true

echo "[start] fluxbox"
fluxbox >/var/log/fluxbox.log 2>&1 &
sleep 1

# VNC：设了 VNC_PASSWORD 就开密码，否则无密码(仅建议本机/内网)
if [ -n "$VNC_PASSWORD" ]; then
  x11vnc -storepasswd "$VNC_PASSWORD" /root/.vncpass >/dev/null 2>&1
  echo "[start] x11vnc :0 -> 5900 (密码保护)"
  x11vnc -display :0 -forever -shared -rfbauth /root/.vncpass -rfbport 5900 -bg -o /var/log/x11vnc.log
else
  echo "[start] x11vnc :0 -> 5900 (无密码! 建议设 VNC_PASSWORD 或只绑内网)"
  x11vnc -display :0 -forever -shared -nopw -rfbport 5900 -bg -o /var/log/x11vnc.log
fi

echo "[start] noVNC -> 6080"
websockify --web=/usr/share/novnc 6080 localhost:5900 >/var/log/novnc.log 2>&1 &
sleep 1

# 定位并拉起微信
WX=""
for c in /opt/wechat/wechat /usr/bin/wechat /opt/tencent/wechat/wechat /usr/local/bin/wechat; do
  [ -x "$c" ] && WX="$c" && break
done
[ -z "$WX" ] && WX=$(command -v wechat || true)
echo "[start] wechat bin = ${WX:-NOT FOUND}"
( "$WX" --no-sandbox >/var/log/wechat.log 2>&1 || "$WX" >/var/log/wechat.log 2>&1 ) &

# 收图秒抢：Frida 常驻看护(rename→硬链接抢图，撤回删原图也不丢)。脚本在 /root(挂载持久)。
CAP=""
for c in /root/frida_capture_supervisor.py /app/docker/frida_capture_supervisor.py; do
  [ -f "$c" ] && CAP="$c" && break
done
if [ -n "$CAP" ]; then
  echo "[start] frida capture supervisor = $CAP"
  ( sleep 15; python3 "$CAP" >/var/log/frida_capture.log 2>&1 ) &
else
  echo "[start] frida capture supervisor NOT FOUND (跳过)"
fi

# 后端(Flask)：与微信同容器，本地驱动 xdotool/解密
echo "[start] backend -> :5100"
mkdir -p /app/accounts
cd /app
export WXBOT_LOCAL=1 WXBOT_BIND=0.0.0.0
( python3 server.py >/var/log/wxbot.log 2>&1 ) &

echo "[start] ready. noVNC http://<host>:6080/vnc.html  后台 http://<host>:5100"
# 保活 + 输出日志便于 docker logs 观察
sleep 2
tail -F /var/log/wxbot.log /var/log/wechat.log 2>/dev/null
