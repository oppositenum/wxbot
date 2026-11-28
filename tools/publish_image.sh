#!/usr/bin/env bash
# 构建 wxbot 镜像(arm64 + amd64)并推送到 Docker Hub(或任何 registry)。
#
#   docker login                                   # 先登录一次
#   ./tools/publish_image.sh <用户名>/wxbot          # 推 <用户名>/wxbot:<微信版本>-<git 短哈希> 和 :latest
#   ./tools/publish_image.sh <用户名>/wxbot v1.2     # 额外指定一个标签
#
# 环境变量:
#   PLATFORMS=linux/arm64          只构建一个架构(默认 linux/arm64,linux/amd64;amd64 在 Mac 上靠 QEMU,较慢)
#   BUILD_PROXY=http://host.docker.internal:7890   构建时 apt/pip/下载微信走的代理(国内网络常需要)
#   NO_PUSH=1                      只构建、不推送(单架构时会 --load 到本机,方便先测)
#
# 微信安装包:Dockerfile 锁定版本和 sha256。腾讯官网升级后直接下载会校验失败,
# 这时把对应版本的 deb 放进 docker/wechat-deb/(见那里的 README)。
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

REPO="${1:-}"
EXTRA_TAG="${2:-}"
[[ -n "$REPO" && "$REPO" =~ ^[a-z0-9._/-]+$ ]] || { echo "用法: $0 <用户名>/wxbot [标签]" >&2; exit 2; }
PLATFORMS="${PLATFORMS:-linux/arm64,linux/amd64}"
WECHAT_VERSION="$(sed -n 's/^ARG WECHAT_VERSION=//p' docker/Dockerfile)"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo local)"
TAGS=(-t "$REPO:$WECHAT_VERSION-$GIT_SHA" -t "$REPO:latest")
[[ -n "$EXTRA_TAG" ]] && TAGS+=(-t "$REPO:$EXTRA_TAG")

if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
  echo "[publish] 注意:工作区有未提交的改动,镜像里会带上它们(标签里的 $GIT_SHA 对不上)" >&2
fi

ARGS=()
if [[ -n "${BUILD_PROXY:-}" ]]; then
  for v in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; do ARGS+=(--build-arg "$v=$BUILD_PROXY"); done
fi

# 多架构要用 docker-container 驱动的 builder
if ! docker buildx inspect wxbot-multiarch >/dev/null 2>&1; then
  docker buildx create --name wxbot-multiarch --driver docker-container --bootstrap >/dev/null
fi

if [[ "${NO_PUSH:-}" == "1" ]]; then
  if [[ "$PLATFORMS" == *,* ]]; then OUT=(); echo "[publish] 多架构 + NO_PUSH:只验证能构建,结果留在 builder 缓存里"
  else OUT=(--load); fi
else
  OUT=(--push)
fi

echo "[publish] 微信 $WECHAT_VERSION,架构 $PLATFORMS,标签: $REPO:$WECHAT_VERSION-$GIT_SHA $REPO:latest $EXTRA_TAG"
docker buildx build --builder wxbot-multiarch --platform "$PLATFORMS" \
  -f docker/Dockerfile ${ARGS[@]+"${ARGS[@]}"} "${TAGS[@]}" ${OUT[@]+"${OUT[@]}"} .

if [[ "${NO_PUSH:-}" != "1" ]]; then
  echo "[publish] 已推送。新机器部署:"
  echo "  git clone <仓库> && cd wxbot && ./deploy.sh $REPO:latest"
fi
