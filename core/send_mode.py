"""回复发送方式:ui=界面自动化(原有),direct=底层直发(实验)。

存 work/send_mode.json,每次发送时读取,改了立即生效,不用重启。
fallback=True 时,直发在"确定没发出"的情况下改走界面;结果不确定时绝不回退(防重复发送)。
"""
import json
import os
import threading
import time

import config

MODES = ("ui", "direct")
DEFAULT = {"mode": "ui", "fallback": True}
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
    if data.get("mode") in MODES:
        out["mode"] = data["mode"]
    if isinstance(data.get("fallback"), bool):
        out["fallback"] = data["fallback"]
    out["updated"] = data.get("updated")
    return out


def mode():
    return get()["mode"]


def set(mode=None, fallback=None):  # noqa: A001 - 模块内语义清楚
    with _lock:
        cur = get()
        if mode is not None:
            if mode not in MODES:
                raise ValueError("mode 只能是 ui 或 direct")
            cur["mode"] = mode
        if fallback is not None:
            cur["fallback"] = bool(fallback)
        cur["updated"] = int(time.time())
        os.makedirs(config.WORK_DIR, exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False)
        os.replace(tmp, _path())
        return cur
