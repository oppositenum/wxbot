# Refresh the legacy image without downloading its Ubuntu desktop base again.
# The resulting image is self-contained; no application source is bind-mounted.
ARG WXBOT_BASE_IMAGE=wxbot-ubuntu-manual:24.04
FROM ${WXBOT_BASE_IMAGE}

COPY --chown=wechat:wechat config.py server.py /app/
COPY --chown=wechat:wechat core/ /app/core/
COPY --chown=wechat:wechat tools/ /app/tools/
COPY --chown=wechat:wechat static/ /app/static/
COPY docker/linux_keys.py docker/wx_send.py docker/wx_send_legacy.py \
     docker/capture_imgkey.py /usr/local/bin/
COPY --chown=wechat:wechat docker/frida_capture.js docker/frida_capture_supervisor.py /app/docker/
COPY docker/start.sh /usr/local/bin/start-wxbot

USER wechat
