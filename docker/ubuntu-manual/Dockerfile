FROM wxbot-ubuntu-base:24.04.4

ENV DEBIAN_FRONTEND=noninteractive TZ=Asia/Shanghai LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8 DISPLAY=:0 SCREEN=1360x900x24
ENV http_proxy="" https_proxy="" HTTP_PROXY="" HTTPS_PROXY="" all_proxy="" ALL_PROXY=""
ARG APT_PROXY=DIRECT
RUN printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\nAcquire::Retries "3";\nAcquire::http::Timeout "30";\nAcquire::Languages "none";\n' "$APT_PROXY" "$APT_PROXY" > /etc/apt/apt.conf.d/99manual-network \
    && sed -i 's|http://ports.ubuntu.com/ubuntu-ports|http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports|g' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
       ca-certificates locales tzdata dbus-x11 xvfb x11vnc novnc websockify \
       xfce4 xfce4-terminal thunar dbus-user-session pulseaudio \
       fcitx5 fcitx5-chinese-addons fcitx5-frontend-gtk3 \
       fonts-noto-cjk fonts-noto-color-emoji \
       libatomic1 libpulse0 libasound2t64 libnss3 libgbm1 \
       libxkbcommon-x11-0 libxcb-xkb1 libxcb-icccm4 libxcb-image0 \
       libxcb-render-util0 libxcb-keysyms1 libxcb-cursor0 libxcb-shape0 \
       libxcb-randr0 libxcb-sync1 libxcb-shm0 libatk1.0-0t64 \
       libatk-bridge2.0-0t64 libatspi2.0-0t64 libcups2t64 libdrm2 \
       libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgtk-3-0t64 \
       libpango-1.0-0 libcairo2 libnspr4 libxshmfence1 libgdk-pixbuf-2.0-0 \
       x11-utils x11-xserver-utils procps \
       xdotool wmctrl scrot xclip tesseract-ocr tesseract-ocr-chi-sim curl python3 python3-pycryptodome \
       python3-zstandard python3-flask python3-pil sqlite3 gdb libcap2-bin \
    && sed -i 's/# *\(zh_CN.UTF-8\)/\1/' /etc/locale.gen && locale-gen \
    && rm -rf /var/lib/apt/lists/* && rm /etc/apt/apt.conf.d/99manual-network

# The management process runs beside this Ubuntu WeChat instance, so it can
# read the same encrypted databases and drive the same X display.  No legacy
# account data is copied into the image; it remains in the named home volume.
RUN mkdir -p /usr/local/lib/python3.12/dist-packages \
    && ln -s /usr/lib/python3/dist-packages/Cryptodome \
       /usr/local/lib/python3.12/dist-packages/Crypto \
    && setcap cap_sys_ptrace+ep /usr/bin/python3.12

COPY docker/WeChatLinux_arm64.deb /tmp/wechat.deb
RUN dpkg -i /tmp/wechat.deb && rm /tmp/wechat.deb \
    && useradd --create-home --shell /bin/bash wechat \
    && mkdir -p /home/wechat/Desktop /home/wechat/Pictures \
    && cp /usr/share/applications/wechat.desktop /home/wechat/Desktop/ \
    && chmod +x /home/wechat/Desktop/wechat.desktop \
    && chown -R wechat:wechat /home/wechat

COPY docker/ubuntu-manual/start.sh /usr/local/bin/start-ubuntu-wechat
RUN chmod +x /usr/local/bin/start-ubuntu-wechat
COPY config.py /app/config.py
COPY server.py /app/server.py
COPY core/ /app/core/
COPY tools/ /app/tools/
COPY static/ /app/static/
COPY docker/linux_keys.py /usr/local/bin/linux_keys.py
COPY docker/wx_send.py /usr/local/bin/wx_send.py
COPY docker/wx_send_legacy.py /usr/local/bin/wx_send_legacy.py
RUN chmod +x /usr/local/bin/wx_send.py /usr/local/bin/wx_send_legacy.py
RUN mkdir -p /app/accounts /app/work && chown -R wechat:wechat /app
USER wechat
WORKDIR /home/wechat
ENV HOME=/home/wechat XMODIFIERS=@im=fcitx GTK_IM_MODULE=fcitx QT_IM_MODULE=fcitx
ENV WXBOT_LOCAL=1 WXBOT_XWECHAT_ROOT=/home/wechat/xwechat_files \
    WXBOT_BIND=0.0.0.0 WXBOT_BOT_AUTOSTART=0 WXBOT_MEDIA_CAPTURE=0 \
    WXBOT_LLM_CONFIG=/home/wechat/llm_config.json PYTHONUNBUFFERED=1
EXPOSE 5900 6080
CMD ["/usr/local/bin/start-ubuntu-wechat"]
