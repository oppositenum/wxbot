#!/usr/bin/env python3
# wxbot 收图秒抢 —— Frida 常驻看护
# -----------------------------------------------------------------------------
# 在容器内运行：attach 到 wechat 主进程，加载 frida_capture.js（rename→硬链接抢图），
# 保持常驻；wechat 退出/重启则自动重连。只 hook rename 家族（低频、非热路径），
# 实测不卡不崩。抢下的字节存 /root/wxbot_capture/<account>/，撤回删原图也不丢。
# -----------------------------------------------------------------------------
import os
import sys
import time
import datetime

import frida

AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frida_capture.js")
# 容器内固定路径兜底（宿主 docker/ = 容器 /root/docker 视挂载而定，用绝对定位）
if not os.path.exists(AGENT):
    for cand in ("/root/frida_capture.js", "/root/docker/frida_capture.js"):
        if os.path.exists(cand):
            AGENT = cand
            break

LOG = "/root/wxbot_capture/supervisor.log"


def log(msg):
    line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs("/root/wxbot_capture", exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def find_wechat(dev):
    for p in dev.enumerate_processes():
        if p.name == "wechat":
            return p.pid
    return None


def on_message(message, data):
    if message.get("type") == "send":
        payload = message.get("payload", {})
        tag = payload.get("tag")
        if tag == "READY":
            log(f"agent READY, capture root={payload.get('root')}")
        elif tag == "CAP":
            log(f"抢到[{payload.get('why')}] {payload.get('dst')}")
    elif message.get("type") == "error":
        log(f"agent error: {message.get('description')}")


def attach_loop():
    src = open(AGENT, "r", encoding="utf-8").read()
    dev = frida.get_local_device()
    session = None
    cur_pid = None
    while True:
        try:
            pid = find_wechat(dev)
            if pid is None:
                if session is not None:
                    log("wechat 已退出，等待重启…")
                    session = None
                    cur_pid = None
                time.sleep(3)
                continue
            if session is not None and pid == cur_pid:
                time.sleep(2)
                continue
            # 新 wechat 进程 → (重新) attach
            log(f"attach wechat pid={pid}")
            session = dev.attach(pid)
            session.on("detached", lambda reason, *a: log(f"detached: {reason}"))
            script = session.create_script(src)
            script.on("message", on_message)
            script.load()
            cur_pid = pid
            log("agent loaded, 常驻抢图中…")
        except frida.ProcessNotFoundError:
            session = None
            cur_pid = None
            time.sleep(3)
        except frida.TransportError:
            session = None
            cur_pid = None
            time.sleep(3)
        except Exception as e:
            log(f"attach 失败: {e!r}，3s 后重试")
            session = None
            cur_pid = None
            time.sleep(3)
        time.sleep(2)


if __name__ == "__main__":
    log(f"supervisor 启动, agent={AGENT}")
    if not os.path.exists(AGENT):
        log("找不到 frida_capture.js，退出")
        sys.exit(1)
    attach_loop()
