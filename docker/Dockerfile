# wxbot production image: Ubuntu desktop + Linux WeChat + Flask backend.
# Build context must be the repository root.
FROM ubuntu:24.04

ARG TARGETARCH
ARG WECHAT_DEB_BASE=https://dldir1v6.qq.com/weixin/Universal/Linux

LABEL org.opencontainers.image.base.name="ubuntu:24.04" \
      com.oppositenum.wxbot.runtime="ubuntu-24.04"

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    LANG=zh_CN.UTF-8 \
    LANGUAGE=zh_CN:zh \
    LC_ALL=zh_CN.UTF-8 \
    DISPLAY=:0 \
    SCREEN=1920x1080x24 \
    HOME=/home/wechat \
    XMODIFIERS=@im=fcitx \
    GTK_IM_MODULE=fcitx \
    QT_IM_MODULE=fcitx \
    PYTHONUNBUFFERED=1 \
    WXBOT_LOCAL=1 \
    WXBOT_BIND=0.0.0.0 \
    WXBOT_XWECHAT_ROOT=/home/wechat/xwechat_files \
    WXBOT_CAPTURE_ROOT=/home/wechat/wxbot_capture \
    WXBOT_IMGKEY_FILE=/home/wechat/imgkey.txt \
    WXBOT_LLM_CONFIG=/app/accounts/llm_config.json

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl locales tzdata \
        dbus-x11 dbus-user-session xvfb x11vnc novnc websockify \
        xfce4 xfce4-terminal thunar pulseaudio ffmpeg \
        fcitx5 fcitx5-chinese-addons fcitx5-frontend-gtk3 \
        fonts-noto-cjk fonts-noto-color-emoji \
        libatomic1 libpulse0 libasound2t64 libnss3 libgbm1 \
        libegl1 libgles2 libgl1 libegl-mesa0 \
        libxkbcommon-x11-0 libxcb-xkb1 libxcb-icccm4 libxcb-image0 \
        libxcb-render-util0 libxcb-keysyms1 libxcb-cursor0 libxcb-shape0 \
        libxcb-randr0 libxcb-sync1 libxcb-shm0 libatk1.0-0t64 \
        libatk-bridge2.0-0t64 libatspi2.0-0t64 libcups2t64 libdrm2 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgtk-3-0t64 \
        libpango-1.0-0 libcairo2 libnspr4 libxshmfence1 \
        libgdk-pixbuf-2.0-0 x11-utils x11-xserver-utils \
        xdotool wmctrl scrot xclip tesseract-ocr tesseract-ocr-chi-sim \
        procps psmisc python3 python3-pip python3-pycryptodome \
        python3-zstandard python3-flask python3-pil sqlite3 gdb libcap2-bin ffmpeg \
    && sed -i 's/# *\(zh_CN.UTF-8\)/\1/' /etc/locale.gen \
    && locale-gen \
    && ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo "${TZ}" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 微信版本锁定:直发(core/direct_send_profiles.py)按微信二进制 sha256 适配,装成别的版本直发会自动停用。
# 腾讯的下载地址永远指向「最新版」,所以下载后校验 sha256;官网升级后这里会构建失败并提示,而不是悄悄换版本。
#   - 离线/固定版本:把 WeChatLinux_arm64.deb / WeChatLinux_x86_64.deb 放进 docker/wechat-deb/ 再构建
#   - 确认要用新版(直发会停用,等重新适配):--build-arg WECHAT_ALLOW_UNPINNED=1
ARG WECHAT_VERSION=4.1.13.29
ARG WECHAT_SHA256_ARM64=b9ee12ba417ff68840d57020ef7014abb23c7b4f4c00ce807672b100c90bc853
ARG WECHAT_SHA256_AMD64=d5b21c6e21186a58b2c2fd41712a4ef90f9452735931a6b8500e85c5b0913b0b
ARG WECHAT_ALLOW_UNPINNED=0
LABEL com.oppositenum.wxbot.wechat="${WECHAT_VERSION}"
COPY docker/wechat-deb/ /tmp/wechat-deb/
RUN set -eux; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      amd64) deb=WeChatLinux_x86_64.deb; want="${WECHAT_SHA256_AMD64}" ;; \
      arm64) deb=WeChatLinux_arm64.deb; want="${WECHAT_SHA256_ARM64}" ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    if [ -f "/tmp/wechat-deb/${deb}" ]; then cp "/tmp/wechat-deb/${deb}" /tmp/wechat.deb; \
    else curl -fSL --retry 8 --retry-delay 3 --retry-all-errors "${WECHAT_DEB_BASE}/${deb}" -o /tmp/wechat.deb; fi; \
    got="$(sha256sum /tmp/wechat.deb | cut -d' ' -f1)"; \
    if [ "$got" != "$want" ]; then \
      if [ "${WECHAT_ALLOW_UNPINNED}" = "1" ]; then echo "WARNING: wechat.deb sha256 $got != pinned ${WECHAT_VERSION}; direct send will be disabled"; \
      else echo "ERROR: wechat.deb sha256 $got is not the pinned WeChat ${WECHAT_VERSION} ($want)." >&2; \
           echo "Tencent probably released a newer version. Put the ${WECHAT_VERSION} deb into docker/wechat-deb/," >&2; \
           echo "or build with --build-arg WECHAT_ALLOW_UNPINNED=1 (direct send stays off until re-adapted)." >&2; exit 1; fi; \
    fi; \
    apt-get update; \
    (apt-get install -y /tmp/wechat.deb || apt-get -f install -y); \
    dpkg-query -W -f='${Version}\n' wechat | tee /etc/wxbot-wechat-version; \
    rm -rf /tmp/wechat.deb /tmp/wechat-deb /var/lib/apt/lists/*

# Live media capture is part of the image. SILK voice decoding is optional
# because pilk may need architecture-specific build tooling.
RUN pip3 install --break-system-packages --no-cache-dir --ignore-installed typing_extensions frida==17.23.3 edge-tts dashscope pycryptodome aiohttp
# pilk(把音频编成微信 SILK,直发语音条用)是 C 扩展:临时装编译器,装完卸掉
RUN apt-get update && apt-get install -y --no-install-recommends gcc python3-dev \
    && pip3 install --break-system-packages --no-cache-dir pilk \
    && apt-get purge -y gcc python3-dev && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash wechat \
    && mkdir -p /app/accounts /app/work /home/wechat/Desktop /home/wechat/Pictures \
    && if [ -f /usr/share/applications/wechat.desktop ]; then \
         cp /usr/share/applications/wechat.desktop /home/wechat/Desktop/; \
         chmod +x /home/wechat/Desktop/wechat.desktop; \
       fi \
    && setcap cap_sys_ptrace+ep /usr/bin/python3.12 \
    && chown -R wechat:wechat /app /home/wechat

WORKDIR /app
COPY --chown=wechat:wechat config.py server.py /app/
COPY --chown=wechat:wechat core/ /app/core/
COPY --chown=wechat:wechat tools/ /app/tools/
COPY --chown=wechat:wechat static/ /app/static/
# 控制台(一个入口管理多个微信;同一镜像,compose 里用 command 启动 console/server.py)
COPY --chown=wechat:wechat console/ /app/console/
COPY docker/linux_keys.py docker/wx_send.py docker/wx_send_legacy.py \
     docker/capture_imgkey.py /usr/local/bin/
COPY docker/frida_capture.js docker/frida_capture_supervisor.py /app/docker/
COPY docker/start.sh /usr/local/bin/start-wxbot
RUN chmod +x /usr/local/bin/start-wxbot \
    /usr/local/bin/wx_send.py /usr/local/bin/wx_send_legacy.py

USER wechat
EXPOSE 5100 5900 6080
CMD ["/usr/local/bin/start-wxbot"]
