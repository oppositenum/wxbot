# wxbot production image: Ubuntu desktop + Linux WeChat + Flask backend.
# Build context must be the repository root.
FROM ubuntu:24.04

ARG TARGETARCH
ARG WECHAT_DEB_BASE=https://dldir1.qq.com/weixin/Universal/Linux

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
        xfce4 xfce4-terminal thunar pulseaudio \
        fcitx5 fcitx5-chinese-addons fcitx5-frontend-gtk3 \
        fonts-noto-cjk fonts-noto-color-emoji \
        libatomic1 libpulse0 libasound2t64 libnss3 libgbm1 \
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

RUN set -eux; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      amd64) deb=WeChatLinux_x86_64.deb ;; \
      arm64) deb=WeChatLinux_arm64.deb ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fSL --retry 8 --retry-delay 3 --retry-all-errors \
      "${WECHAT_DEB_BASE}/${deb}" -o /tmp/wechat.deb; \
    apt-get update; \
    (apt-get install -y /tmp/wechat.deb || apt-get -f install -y); \
    rm -f /tmp/wechat.deb; \
    rm -rf /var/lib/apt/lists/*

# Live media capture is part of the image. SILK voice decoding is optional
# because pilk may need architecture-specific build tooling.
RUN pip3 install --break-system-packages --no-cache-dir frida==17.17.0
RUN pip3 install --break-system-packages --no-cache-dir pilk || true

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
COPY docker/linux_keys.py docker/wx_send.py docker/wx_send_legacy.py \
     docker/capture_imgkey.py /usr/local/bin/
COPY docker/frida_capture.js docker/frida_capture_supervisor.py /app/docker/
COPY docker/start.sh /usr/local/bin/start-wxbot
RUN chmod +x /usr/local/bin/start-wxbot \
    /usr/local/bin/wx_send.py /usr/local/bin/wx_send_legacy.py

USER wechat
EXPOSE 5100 5900 6080
CMD ["/usr/local/bin/start-wxbot"]
