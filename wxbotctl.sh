#!/usr/bin/env bash
# wxbot 本地服务控制脚本
# 用法：
#   ./wxbotctl.sh start              启动已有容器
#   ./wxbotctl.sh stop               停止容器（保留数据和登录态）
#   ./wxbotctl.sh restart            重启容器
#   ./wxbotctl.sh status             查看容器和端口状态
#   ./wxbotctl.sh logs [行数]         查看最近日志
#
# 可通过环境变量覆盖：
#   WXBOT_CONTAINER=wxbot-ubuntu-manual
#   WXBOT_PORT=5100

set -Eeuo pipefail

CONTAINER="${WXBOT_CONTAINER:-wxbot-ubuntu-manual}"
PORT="${WXBOT_PORT:-5100}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "[wxbot] 错误：$*" >&2; exit 1; }
need_docker() { command -v docker >/dev/null 2>&1 || die "找不到 docker，请先启动 Docker Desktop。"; }
exists() { docker container inspect "${CONTAINER}" >/dev/null 2>&1; }
has_backend_port() {
  docker port "${CONTAINER}" 2>/dev/null | grep -Eq "(^|:)${PORT}->|:${PORT}->"
}

status() {
  need_docker
  if ! exists; then
    echo "[wxbot] 容器不存在：${CONTAINER}"
    return 1
  fi
  if ! has_backend_port; then
    echo "[wxbot] 容器 ${CONTAINER} 没有映射后台端口 ${PORT}。"
    echo "[wxbot] 当前容器可能只是微信/noVNC 容器；请指定真正运行 server.py 的容器："
    echo "        WXBOT_CONTAINER=<容器名> $0 status"
    return 1
  fi
  docker ps -a --filter "name=^/${CONTAINER}$" \
    --format '容器={{.Names}} 状态={{.Status}} 镜像={{.Image}}'
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || echo "端口 $PORT 当前未监听"
  fi
}

start() {
  need_docker
  if ! exists; then
    die "容器 ${CONTAINER} 不存在。请先按部署文档创建容器，脚本不会自动删除或重建数据容器。"
  fi
  has_backend_port || die "容器 ${CONTAINER} 没有映射后台端口 ${PORT}，不会把微信/noVNC 容器当作后台服务启动。"
  local state
  state="$(docker inspect -f '{{.State.Status}}' "${CONTAINER}")"
  if [[ "$state" == "running" ]]; then
    echo "[wxbot] 已在运行：${CONTAINER}"
    status || true
    return 0
  fi
  echo "[wxbot] 启动：${CONTAINER}"
  docker start "${CONTAINER}" >/dev/null
  sleep 2
  state="$(docker inspect -f '{{.State.Status}}' "${CONTAINER}")"
  if [[ "$state" != "running" ]]; then
    echo "[wxbot] 启动失败，最近日志：" >&2
    docker logs --tail 80 "${CONTAINER}" >&2 || true
    return 1
  fi
  echo "[wxbot] 已启动，后台地址：http://127.0.0.1:${PORT}"
  status || true
}

stop() {
  need_docker
  if ! exists; then
    echo "[wxbot] 容器不存在：${CONTAINER}"
    return 0
  fi
  local state
  state="$(docker inspect -f '{{.State.Status}}' "${CONTAINER}")"
  if [[ "$state" != "running" ]]; then
    echo "[wxbot] 已停止：${CONTAINER}（数据卷未删除）"
    return 0
  fi
  echo "[wxbot] 停止：${CONTAINER}"
  docker stop -t 20 "${CONTAINER}" >/dev/null
  echo "[wxbot] 已停止，数据和登录态保留"
}

restart() { stop; start; }

logs() {
  need_docker
  exists || die "容器不存在：${CONTAINER}"
  docker logs --tail "${1:-100}" -f "${CONTAINER}"
}

usage() {
  cat <<EOF
用法：$0 {start|stop|restart|status|logs [行数]}

示例：
  $0 start
  $0 stop
  $0 restart
  $0 status
  $0 logs 200
EOF
}

cd "$ROOT_DIR"
case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) restart ;;
  status) status ;;
  logs) shift; logs "${1:-100}" ;;
  *) usage; exit 2 ;;
esac
