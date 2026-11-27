# Refresh the legacy image without downloading its Ubuntu desktop base again.
# The resulting image is self-contained; no application source is bind-mounted.
ARG WXBOT_BASE_IMAGE=wxbot-ubuntu-manual:24.04
FROM ${WXBOT_BASE_IMAGE}

# export/import bases lose image ENV; restore the desktop/backend defaults the
# start script and compose expect. Values from env_file/.env still override.
ENV DISPLAY=:0 \
    SCREEN=1920x1080x24 \
    TZ=Asia/Shanghai \
    LANG=zh_CN.UTF-8 \
    LANGUAGE=zh_CN:zh \
    LC_ALL=zh_CN.UTF-8 \
    WXBOT_LOCAL=1 \
    WXBOT_BIND=0.0.0.0 \
    WXBOT_XWECHAT_ROOT=/home/wechat/xwechat_files \
    WXBOT_LLM_CONFIG=/home/wechat/llm_config.json \
    PYTHONUNBUFFERED=1

# 直发"语音":Linux 版微信发不了语音条,音频用 ffmpeg 转成带声波画面的视频再发
USER root
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=wechat:wechat config.py server.py /app/
COPY --chown=wechat:wechat core/ /app/core/
COPY --chown=wechat:wechat tools/ /app/tools/
COPY --chown=wechat:wechat static/ /app/static/
COPY docker/linux_keys.py docker/wx_send.py docker/wx_send_legacy.py \
     docker/capture_imgkey.py /usr/local/bin/
COPY --chown=wechat:wechat docker/frida_capture.js docker/frida_capture_supervisor.py /app/docker/
COPY docker/start.sh /usr/local/bin/start-wxbot
COPY docker/ubuntu-manual/start-ubuntu-wechat /usr/local/bin/start-ubuntu-wechat

USER wechat
