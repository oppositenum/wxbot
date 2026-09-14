"""拟人化节奏控制 —— 让自动回复不再"瞬发/秒回",降低机器/骚扰风控特征。

集中所有回复节奏逻辑:打字延迟、去抖窗口抖动、深夜变慢/偶尔不回、战斗模式限速。
参数默认见 DEFAULTS,可被 bot_rules.json 的 `humanize` 段覆盖(rules 每轮热加载)。
per-chat 节奏状态用进程内 dict(重启即清,无持久化)。
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
    # 打字延迟(温和档):base + per_char*字数,乘深夜系数,叠抖动,clamp 到 [min, cap]
    "per_char": 0.15,
    "base": 0.6,
    "min": 0.8,
    "cap": 15.0,
    "jitter": 0.25,        # ±25% 随机抖动
    # 深夜时段 [night_start, night_end)(本地 Asia/Shanghai 小时)
    "night_start": 0,
    "night_end": 7,
    "night_factor": 1.6,   # 深夜延迟拉长倍数
    "night_drop_prob": 0.08,  # 深夜普通回复"偶尔不回"概率(战斗模式不受此影响)
    # 去抖窗口抖动倍数(每批取一次)
    "settle_min": 1.0,
    "settle_max": 1.8,
    # 战斗模式限速:两次回击最小间隔随机 [gap_min, gap_max] 秒 + 软小时上限
    "battle_gap_min": 15.0,
    "battle_gap_max": 40.0,
    "battle_hourly_cap": 40,
}

# 允许 rules 覆盖的运行时配置(load 时刷新)
_cfg = dict(DEFAULTS)

# per-chat 战斗回击时间戳(单调时钟秒),用于最小间隔;小时窗口用挂钟秒
_battle_last = {}          # chat -> monotonic ts of last dispatched battle reply
_battle_hits = {}          # chat -> list[wallclock ts] in the last hour


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
    """按字数模拟真人敲字所需秒数:base + per_char*len,×深夜系数,±抖动,clamp。"""
    n = len((text or "").strip())
    secs = _cfg["base"] + _cfg["per_char"] * n
    secs *= night_factor(now)
    j = _cfg["jitter"]
    if j:
        secs *= random.uniform(1 - j, 1 + j)
    return max(_cfg["min"], min(_cfg["cap"], secs))


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
    gap = random.uniform(_cfg["battle_gap_min"], _cfg["battle_gap_max"])
    hits = _prune_hits(chat, wall)
    if len(hits) >= _cfg["battle_hourly_cap"]:
        gap *= 3   # 超小时上限:退避而非丢弃
    return (mono - last) >= gap


def battle_mark(chat, now=None):
    """记录一次战斗回击的派发时间(供 battle_ready 计间隔与小时计数)。"""
    _battle_last[chat] = time.monotonic()
    wall = time.time() if now is None else now
    _battle_hits.setdefault(chat, []).append(wall)
    _prune_hits(chat, wall)


def _reset_for_test():
    """单测隔离:清空 per-chat 状态并恢复默认参数。"""
    global _cfg
    _cfg = dict(DEFAULTS)
    _battle_last.clear()
    _battle_hits.clear()
