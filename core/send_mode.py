"""回复发送方式:ui=界面自动化(原有),direct=底层直发(实验)。

存 work/send_mode.json,每次发送时读取,改了立即生效,不用重启。
fallback=True 时,直发在"确定没发出"的情况下改走界面;结果不确定时绝不回退(防重复发送)。
下面几个开关只有直发 + 微信 4.1.13 能做,做不到时照常发(不引用/不 @):
  group_quote  机器人在群里回复某人,第一条引用对方那句话
  group_at     群里最近有多个人在说话时,回复第一条 @ 要回答的那个人
  draw_quote   画图发图前,那句「在画了」引用对方要图的那条消息
"""
import json
import os
import threading
import time

import config

MODES = ("ui", "direct")
FLAGS = ("fallback", "group_quote", "group_at", "draw_quote")
DEFAULT = {"mode": "ui", "fallback": True, "group_quote": False, "group_at": False, "draw_quote": False}
_lock = threading.Lock()


def _path():
    return os.path.join(config.WORK_DIR, "send_mode.json")


def get():
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    out = dict(DEFAULT)
    if os.environ.get("WXBOT_SEND_MODE") in MODES:      # 新容器的默认值(控制台创建的号默认直发)
        out["mode"] = os.environ["WXBOT_SEND_MODE"]
    if data.get("mode") in MODES:
        out["mode"] = data["mode"]
    for k in FLAGS:
        if isinstance(data.get(k), bool):
            out[k] = data[k]
    out["updated"] = data.get("updated")
    return out


def mode():
    return get()["mode"]


def set(mode=None, fallback=None, group_quote=None, **flags):  # noqa: A001 - 模块内语义清楚
    with _lock:
        cur = get()
        if mode is not None:
            if mode not in MODES:
                raise ValueError("mode 只能是 ui 或 direct")
            cur["mode"] = mode
        flags.update(fallback=fallback, group_quote=group_quote)
        for k, v in flags.items():
            if k not in FLAGS:
                raise ValueError(f"未知开关:{k}")
            if v is not None:
                cur[k] = bool(v)
        cur["updated"] = int(time.time())
        os.makedirs(config.WORK_DIR, exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False)
        os.replace(tmp, _path())
        return cur
