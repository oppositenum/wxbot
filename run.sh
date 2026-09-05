#!/bin/bash
# wxbot 一键启动（Docker/Linux 微信 方案）
#   1) 确保容器在跑（noVNC 扫码登录小号）
#   2) 从容器内存提取密钥 + 解密
#   3) 启动后台 http://localhost:5100
set -e
cd "$(dirname "$0")"
IMG=wxbot-wechat
NAME=wxbot

# 1. 容器
if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "[run] 启动容器 $NAME ..."
  docker rm -f "$NAME" 2>/dev/null || true
  docker run -d --name "$NAME" --platform linux/arm64 \
    --shm-size=1g --security-opt seccomp=unconfined \
    -p 6080:6080 -p 5901:5900 \
    -v "$PWD/docker/wxdata:/root" "$IMG"
  sleep 8
fi

# 2. 登录检查
if [ -z "$(docker exec $NAME bash -c 'ls /root/xwechat_files/*/db_storage/contact/contact.db 2>/dev/null')" ]; then
  echo "[run] 尚未登录。请打开 http://localhost:6080/vnc.html 用小号扫码，登录后重跑本脚本。"
  exit 0
fi

# 3. 取密钥 + 解密
WXID=$(docker exec $NAME bash -c "ls /root/xwechat_files 2>/dev/null | head -1")
echo "[run] 账号 $WXID，提取密钥 ..."
docker exec $NAME python3 /usr/local/bin/linux_keys.py \
  "/root/xwechat_files/$WXID/db_storage" /root/keys.json
echo "[run] 解密 ..."
python3 -m core.decrypt

# 4. 后台
echo "[run] 启动后台 http://localhost:5100"
python3 server.py
