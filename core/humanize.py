"""拟人化节奏控制 —— 让自动回复不再"瞬发/秒回",降低机器/骚扰风控特征。

集中所有回复节奏逻辑:打字延迟、去抖窗口抖动、深夜变慢、账号级发送间隔、战斗模式限速。
参数默认见 DEFAULTS,可被 bot_rules.json 的 `humanize` 段覆盖(rules 每轮热加载)。
per-chat / 账号节奏状态用进程内 dict(重启即清,无持久化)。
"""

import os
import random
import time

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - zoneinfo 总在,兜底避免导入炸
    _TZ = None

DEFAULTS = {
    # 节奏限制默认全关:不打字等待、不随机丢回复、不限发送间隔。
    "per_char": 0.0,
    "base": 0.0,
    "min": 0.0,
    "cap": 0.0,
    "jitter": 0.0,
    "night_start": 0,
    "night_end": 7,
    "night_factor": 1.0,
    "night_drop_prob": 0.0,
    "settle_min": 1.0,
    "settle_max": 1.0,
    "min_send_gap": 0.0,
    "hourly_cap": 0,       # 0 = 不限
    "battle_gap_min": 0.0,
    "battle_gap_max": 0.0,
    "battle_hourly_cap": 0,
}

# 允许 rules 覆盖的运行时配置(load 时刷新)
_cfg = dict(DEFAULTS)

# per-chat 战斗回击时间戳(单调时钟秒),用于最小间隔;小时窗口用挂钟秒
_battle_last = {}          # chat -> monotonic ts of last dispatched battle reply
_battle_hits = {}          # chat -> list[wallclock ts] in the last hour
_send_last = None          # monotonic ts of last account-level UI send
_send_hits = []            # wallclock ts of account-level UI sends in the last hour


def configure(rules):
    """从 rules['humanize'] 覆盖默认参数(缺省沿用 DEFAULTS)。每轮 run_once 调一次即可。"""
    global _cfg
    merged = dict(DEFAULTS)
    over = (rules or {}).get("humanize") or {}
    for k, v in over.items():
        if k in merged and isinstance(v, (int, float)):
            merged[k] = v
    _cfg = merged
    return _cfg


def _now_hour(now=None):
    """返回 Asia/Shanghai 当前小时(0-23)。now 为 unix 秒或 None。"""
    ts = time.time() if now is None else now
    if _TZ is not None:
        from datetime import datetime
        return datetime.fromtimestamp(ts, _TZ).hour
    # 兜底:UTC+8
    return int((ts + 8 * 3600) // 3600 % 24)


def is_night(now=None):
    h = _now_hour(now)
    s, e = _cfg["night_start"], _cfg["night_end"]
    if s <= e:
        return s <= h < e
    return h >= s or h < e   # 跨零点区间(如 22-6)


def night_factor(now=None):
    return _cfg["night_factor"] if is_night(now) else 1.0


def typing_delay(text, chat=None, now=None):
    """按字数模拟真人敲字所需秒数。cap<=0 表示不延迟。"""
    if _cfg["cap"] <= 0 and _cfg["min"] <= 0:
        return 0.0
    n = len((text or "").strip())
    secs = _cfg["base"] + _cfg["per_char"] * n
    secs *= night_factor(now)
    j = _cfg["jitter"]
    if j:
        secs *= random.uniform(1 - j, 1 + j)
    lo, hi = _cfg["min"], _cfg["cap"]
    if hi <= 0:
        return max(0.0, lo)
    return max(lo, min(hi, secs))


def settle_factor():
    """去抖窗口的随机倍数(每批取一次并缓存,避免逐轮跳变)。"""
    return random.uniform(_cfg["settle_min"], _cfg["settle_max"])


def night_drop(chat=None, now=None):
    """仅普通回复用:深夜以小概率返回 True = 这批不回(变慢不静默里的"偶尔不回")。"""
    if not is_night(now):
        return False
    return random.random() < _cfg["night_drop_prob"]


def _prune_hits(chat, wall):
    hits = [t for t in _battle_hits.get(chat, []) if wall - t < 3600]
    _battle_hits[chat] = hits
    return hits


def battle_ready(chat, now=None):
    """战斗回击限速:距上次回击够最小间隔才允许派发(不跳过内容——未就绪则留批累积)。

    最小间隔在 [gap_min, gap_max] 随机;当前小时回击数超软上限则加大间隔退避。
    """
    mono = time.monotonic()
    wall = time.time() if now is None else now
    last = _battle_last.get(chat)
    if last is None:
        return True
    lo, hi = _cfg["battle_gap_min"], _cfg["battle_gap_max"]
    if hi <= 0:
        gap = 0.0
    else:
        gap = random.uniform(lo, hi)
    hits = _prune_hits(chat, wall)
    cap = _cfg["battle_hourly_cap"]
    if cap and len(hits) >= cap:
        gap *= 3   # 超小时上限:退避而非丢弃
    return (mono - last) >= gap


def battle_mark(chat, now=None):
    """记录一次战斗回击的派发时间(供 battle_ready 计间隔与小时计数)。"""
    _battle_last[chat] = time.monotonic()
    wall = time.time() if now is None else now
    _battle_hits.setdefault(chat, []).append(wall)
    _prune_hits(chat, wall)


def _prune_sends(wall):
    global _send_hits
    _send_hits = [t for t in _send_hits if wall - t < 3600]
    return _send_hits


def send_ready(now=None):
    """账号级小时上限:满了先不派发新批次,待处理消息留在队列里。hourly_cap<=0 不限。"""
    cap = _cfg["hourly_cap"]
    if not cap:
        return True
    wall = time.time() if now is None else now
    return len(_prune_sends(wall)) < cap


def wait_gap():
    """两条自动发送之间补齐最小间隔。min_send_gap<=0 时不睡。"""
    last = _send_last
    gap = _cfg["min_send_gap"]
    if last is None or gap <= 0:
        return 0.0
    need = gap - (time.monotonic() - last)
    if need <= 0:
        return 0.0
    time.sleep(min(need, gap))
    return need


def send_mark(now=None):
    """记录一次真实界面发送(供间隔与小时计数)。"""
    global _send_last
    _send_last = time.monotonic()
    wall = time.time() if now is None else now
    _prune_sends(wall).append(wall)


def _reset_for_test():
    """单测隔离:清空 per-chat 状态并恢复默认参数。"""
    global _cfg, _send_last
    _cfg = dict(DEFAULTS)
    _battle_last.clear()
    _battle_hits.clear()
    _send_last = None
    _send_hits.clear()
