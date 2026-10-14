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


def test_typing_delay_off_by_default():
    assert humanize.typing_delay("嗯", now=DAY) == 0.0
    assert humanize.typing_delay("字" * 500, now=NIGHT) == 0.0
    humanize.configure({"humanize": {"min": 0.8, "cap": 15, "base": 0.6, "per_char": 0.15, "jitter": 0}})
    assert humanize.typing_delay("嗯", now=DAY) >= 0.8
    assert humanize.typing_delay("字" * 500, now=DAY) <= 15


def test_typing_delay_night_longer():
    humanize.configure({"humanize": {"min": 0.5, "cap": 20, "base": 1, "per_char": 0.1,
                                     "jitter": 0, "night_factor": 1.6}})
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
    assert humanize.DEFAULTS["night_drop_prob"] == 0.0   # 陪聊默认不随机丢
    with mock.patch("core.humanize.random.random", return_value=0.01):
        assert humanize.night_drop("c", now=NIGHT) is False
    humanize.configure({"humanize": {"night_drop_prob": 0.08}})
    with mock.patch("core.humanize.random.random", return_value=0.01):
        assert humanize.night_drop("c", now=NIGHT) is True    # < prob → 跳过
    with mock.patch("core.humanize.random.random", return_value=0.99):
        assert humanize.night_drop("c", now=NIGHT) is False   # >= prob → 不跳过


def test_account_send_gap_and_hourly_cap_off_by_default():
    assert humanize.send_ready(now=DAY) is True
    with mock.patch("core.humanize.time.sleep") as slept:
        humanize.send_mark(now=DAY)
        assert humanize.wait_gap() == 0.0
        slept.assert_not_called()
    humanize.configure({"humanize": {"hourly_cap": 4, "min_send_gap": 8}})
    with mock.patch("core.humanize.time.sleep") as slept:
        humanize.send_mark(now=DAY)
        humanize.wait_gap()
        slept.assert_called_once()
    for i in range(4):
        humanize.send_mark(now=DAY)
    assert humanize.send_ready(now=DAY) is False
    assert humanize.send_ready(now=DAY + 3601) is True


def test_battle_ready_unlimited_by_default():
    chat = "g@chatroom"
    assert humanize.battle_ready(chat) is True
    humanize.battle_mark(chat)
    with mock.patch("core.humanize.time.monotonic", return_value=time.monotonic()):
        assert humanize.battle_ready(chat) is True
    humanize.configure({"humanize": {"battle_gap_min": 15, "battle_gap_max": 40, "battle_hourly_cap": 40}})
    humanize.battle_mark(chat)
    with mock.patch("core.humanize.time.monotonic", return_value=time.monotonic()):
        assert humanize.battle_ready(chat) is False


def test_battle_hourly_cap_backoff():
    chat = "g2@chatroom"
    humanize.configure({"humanize": {"battle_gap_min": 15, "battle_gap_max": 40, "battle_hourly_cap": 40}})
    cap = 40
    base_mono = 1000.0
    for i in range(cap):
        with mock.patch("core.humanize.time.monotonic", return_value=base_mono + i * 100):
            humanize.battle_mark(chat, now=DAY + i)
    last_mono = base_mono + (cap - 1) * 100
    probe = last_mono + 42
    with mock.patch("core.humanize.time.monotonic", return_value=probe):
        assert humanize.battle_ready(chat, now=DAY) is False


def test_configure_override():
    humanize.configure({"humanize": {"per_char": 1.0, "cap": 99}})
    assert humanize._cfg["per_char"] == 1.0
    assert humanize._cfg["cap"] == 99
    # 未覆盖项沿用默认
    assert humanize._cfg["base"] == 0.0
    # 非法/未知键忽略
    humanize.configure({"humanize": {"bogus": 5, "per_char": "x"}})
    assert humanize._cfg["per_char"] == humanize.DEFAULTS["per_char"]
