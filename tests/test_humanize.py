"""core.humanize 拟人化节奏离线单测。"""

import time
from unittest import mock

import pytest

from core import humanize


@pytest.fixture(autouse=True)
def _reset():
    humanize._reset_for_test()
    yield
    humanize._reset_for_test()


# 2026-09-14 是周一;取一个白天与深夜的 unix 秒(Asia/Shanghai)。
# 12:00 CST == 04:00 UTC;03:00 CST == 19:00 前一天 UTC。用构造法更稳:
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    _CST = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover
    _CST = None


def _ts(hour):
    return datetime(2026, 9, 14, hour, 0, 0, tzinfo=_CST).timestamp()


DAY = _ts(12)      # 中午
NIGHT = _ts(3)     # 凌晨三点


def test_night_factor_boundaries():
    assert humanize.is_night(_ts(6)) is True     # 06:00 仍深夜
    assert humanize.is_night(_ts(7)) is False     # 07:00 白天
    assert humanize.night_factor(NIGHT) == humanize.DEFAULTS["night_factor"]
    assert humanize.night_factor(DAY) == 1.0


def test_typing_delay_bounds():
    d = humanize.DEFAULTS
    # 短文本不低于 min
    assert humanize.typing_delay("嗯", now=DAY) >= d["min"]
    # 长文本被 cap 截住
    long = "字" * 500
    assert humanize.typing_delay(long, now=DAY) <= d["cap"]
    # 空/None 也安全
    assert d["min"] <= humanize.typing_delay("", now=DAY) <= d["cap"]
    assert d["min"] <= humanize.typing_delay(None, now=DAY) <= d["cap"]


def test_typing_delay_night_longer():
    # 关掉抖动后,深夜应严格更慢(同文本)
    with mock.patch("core.humanize.random.uniform", return_value=1.0):
        text = "一段中等长度的测试文本用来对比昼夜"
        assert humanize.typing_delay(text, now=NIGHT) > humanize.typing_delay(text, now=DAY)


def test_settle_factor_range():
    d = humanize.DEFAULTS
    for _ in range(50):
        f = humanize.settle_factor()
        assert d["settle_min"] <= f <= d["settle_max"]


def test_night_drop():
    assert humanize.night_drop("c", now=DAY) is False    # 白天恒不跳过
    with mock.patch("core.humanize.random.random", return_value=0.01):
        assert humanize.night_drop("c", now=NIGHT) is True    # < prob → 跳过
    with mock.patch("core.humanize.random.random", return_value=0.99):
        assert humanize.night_drop("c", now=NIGHT) is False   # >= prob → 不跳过


def test_battle_ready_min_gap():
    chat = "g@chatroom"
    assert humanize.battle_ready(chat) is True    # 首次总允许
    humanize.battle_mark(chat)
    # 刚回击完,最小间隔内不允许(patch monotonic 让时间几乎不动)
    with mock.patch("core.humanize.time.monotonic", return_value=time.monotonic()):
        assert humanize.battle_ready(chat) is False
    # 快进超过最大间隔 → 允许
    future = time.monotonic() + humanize.DEFAULTS["battle_gap_max"] + 5
    with mock.patch("core.humanize.time.monotonic", return_value=future):
        assert humanize.battle_ready(chat) is True


def test_battle_hourly_cap_backoff():
    chat = "g2@chatroom"
    cap = humanize.DEFAULTS["battle_hourly_cap"]
    base_mono = 1000.0
    # 塞满小时上限的回击(都在同一秒,间隔用 monotonic 控制)
    for i in range(cap):
        with mock.patch("core.humanize.time.monotonic", return_value=base_mono + i * 100):
            humanize.battle_mark(chat, now=DAY + i)
    # 距上次 gap_max 秒:未超上限本应允许,但超上限后需 3×gap 退避 → 仍不允许
    last_mono = base_mono + (cap - 1) * 100
    probe = last_mono + humanize.DEFAULTS["battle_gap_max"] + 2
    with mock.patch("core.humanize.time.monotonic", return_value=probe):
        assert humanize.battle_ready(chat, now=DAY) is False


def test_configure_override():
    humanize.configure({"humanize": {"per_char": 1.0, "cap": 99}})
    assert humanize._cfg["per_char"] == 1.0
    assert humanize._cfg["cap"] == 99
    # 未覆盖项沿用默认
    assert humanize._cfg["base"] == humanize.DEFAULTS["base"]
    # 非法/未知键忽略
    humanize.configure({"humanize": {"bogus": 5, "per_char": "x"}})
    assert humanize._cfg["per_char"] == humanize.DEFAULTS["per_char"]
