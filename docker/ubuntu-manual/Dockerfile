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

# 语音:ffmpeg 把音频转成声波视频;edge-tts 做免费的文字转语音
USER root
# pilk:把音频编码成微信 SILK,直发语音条用(C 扩展,构建时临时装编译器)
# EGL/GLES:微信 4.1.13 发视频要用它生成封面,缺了视频必失败,而且会把之后所有图片/文件上传拖坏
# 基础镜像里 python3.12 带 cap_sys_ptrace(让 wechat 用户的 Frida 能附加微信),
# buildkit 构建沙箱不允许执行带额外 capability 的程序(Operation not permitted),
# 所以装包期间先去掉,装完再恢复。
RUN setcap -r /usr/bin/python3.12 \
    && apt-get update && apt-get install -y --no-install-recommends ffmpeg python3-pip \
       libegl1 libgles2 libgl1 libegl-mesa0 \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --break-system-packages --no-cache-dir --ignore-installed typing_extensions edge-tts dashscope \
    && apt-get update && apt-get install -y --no-install-recommends gcc python3-dev \
    && python3 -m pip install --break-system-packages --no-cache-dir pilk \
    && apt-get purge -y gcc python3-dev && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
    && setcap cap_sys_ptrace=ep /usr/bin/python3.12

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
