# wxbot 一体化镜像：Linux 微信(Xvfb+noVNC) + 后端(Flask) 同容器
# 架构自适应：buildx 传入 TARGETARCH(amd64/arm64)，自动下载对应的微信 deb。
# 构建须以【项目根目录】为 context： docker buildx build -f docker/Dockerfile .
FROM debian:12

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    LANG=zh_CN.UTF-8 \
    LANGUAGE=zh_CN:zh \
    LC_ALL=zh_CN.UTF-8 \
    DISPLAY=:0 \
    SCREEN=1360x900x24

# 关闭构建期代理，用阿里云 Debian 镜像(国内构建更快；海外可传 --build-arg 换回)
ENV http_proxy="" https_proxy="" HTTP_PROXY="" HTTPS_PROXY="" all_proxy="" ALL_PROXY=""
ARG APT_MIRROR=mirrors.aliyun.com
RUN (sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true) \
    && (sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list 2>/dev/null || true) \
    && printf 'Acquire::Retries "8";\nAcquire::http::Timeout "30";\n' > /etc/apt/apt.conf.d/80retries

# 桌面/VNC/工具 + 中文字体 + 自动化(xdotool) + 解密/后端(python/sqlite) + 语音转码(ffmpeg)
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc fluxbox novnc websockify \
        dbus-x11 x11-utils x11-xserver-utils xdotool wmctrl scrot xterm xclip \
        fonts-wqy-zenhei fonts-wqy-microhei fonts-noto-color-emoji \
        libnss3 libgbm1 libasound2 \
        procps psmisc curl wget ca-certificates locales \
        python3 python3-pip sqlite3 gdb ffmpeg \
    && sed -i 's/# *\(zh_CN.UTF-8\)/\1/' /etc/locale.gen && locale-gen \
    && rm -rf /var/lib/apt/lists/*

# WeChat 运行时依赖：主程序 + WeChatAppEx(CEF/GTK 栈)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libatomic1 libpulse0 \
        libxkbcommon-x11-0 libxcb-xkb1 libxcb-icccm4 libxcb-image0 \
        libxcb-render-util0 libxcb-keysyms1 libxcb-cursor0 libxcb-shape0 \
        libxcb-randr0 libxcb-sync1 libxcb-shm0 \
        libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 libcups2 libdrm2 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgtk-3-0 \
        libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libnss3 libnspr4 \
        libxshmfence1 libxkbcommon0 libgdk-pixbuf-2.0-0 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 安装微信：按目标架构下载对应 deb(buildx 自动注入 TARGETARCH)
ARG TARGETARCH
ARG WECHAT_DEB_BASE=https://dldir1.qq.com/weixin/Universal/Linux
RUN set -eux; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      amd64) DEB=WeChatLinux_x86_64.deb ;; \
      arm64) DEB=WeChatLinux_arm64.deb ;; \
      *) echo "unsupported arch: ${TARGETARCH}"; exit 1 ;; \
    esac; \
    curl -fSL --retry 8 --retry-delay 3 --retry-all-errors "${WECHAT_DEB_BASE}/${DEB}" -o /tmp/wechat.deb; \
    apt-get update; \
    (apt-get install -y /tmp/wechat.deb || apt-get -f install -y); \
    rm -f /tmp/wechat.deb; rm -rf /var/lib/apt/lists/*

# Python 依赖(容器内做 解密/读取/后端/图片检测)——必需项
RUN pip3 install --break-system-packages --no-cache-dir \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        pycryptodome zstandard flask pillow \
    || pip3 install --break-system-packages --no-cache-dir \
        pycryptodome zstandard flask pillow
# pilk(SILK 语音解码)是 C 扩展，缺编译器时跳过——语音为可选功能，缺了不影响其余
RUN pip3 install --break-system-packages --no-cache-dir \
        -i https://mirrors.aliyun.com/pypi/simple/ pilk || true

# 后端应用代码
WORKDIR /app
COPY config.py /app/config.py
COPY server.py /app/server.py
COPY core/ /app/core/
COPY static/ /app/static/

# 容器内工具：取密钥 + 发送
COPY docker/linux_keys.py /usr/local/bin/linux_keys.py
COPY docker/wx_send.py /usr/local/bin/wx_send.py
COPY docker/start.sh /start.sh
RUN chmod +x /start.sh /usr/local/bin/wx_send.py

# 后端与微信同容器：命令本地执行；数据/配置放持久化卷
ENV WXBOT_LOCAL=1 \
    WXBOT_XWECHAT_ROOT=/root/xwechat_files \
    WXBOT_LLM_CONFIG=/app/accounts/llm_config.json \
    PYTHONUNBUFFERED=1

EXPOSE 5100 5900 6080
CMD ["/start.sh"]
