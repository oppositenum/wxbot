from unittest.mock import patch
from core import niu_mode, bot


def test_payload_strips_trigger():
    assert niu_mode.payload("牛来 你现在是谁") == "你现在是谁"
    assert niu_mode.payload("牛来你现在是谁") == "你现在是谁"
    assert niu_mode.payload("牛来：你现在是谁") == "你现在是谁"
    assert niu_mode.payload("你好") is None
    assert niu_mode.is_trigger({"type": 1, "content": "牛来"}) is False
    assert niu_mode.is_trigger({"type": 1, "content": "牛来 你现在是谁"}) is True
    assert niu_mode.is_trigger({"type": 1, "content": "[牛牛模式]黎深", "is_self": True}) is False


def test_niu_uses_short_llm_path():
    captured = {}
    msg = {"type": 1, "content": "你现在是谁", "is_self": False, "sender": "niu:x", "_niu": True, "local_id": 1}
    orig = {"type": 1, "content": "牛来 你现在是谁", "is_self": True, "sender": "me", "local_id": 1}
    persona = {"name": "黎深", "persona": "你是黎深。"}
    def fake_chat(system, messages, cfg=None):
        captured["system"] = system
        captured["messages"] = messages
        captured["cfg"] = cfg
        return "我是黎深"
    with patch.object(bot.llm, "chat", fake_chat), \
         patch.object(bot.personalization, "maybe_switch", return_value=None):
        out = bot._ai_reply(persona, "g@chatroom", orig, [orig], rules={}, batch_msgs=[orig])
    assert out == "我是黎深"
    assert "牛来" not in captured["system"] or "点名" in captured["system"]
    assert captured["messages"][0]["content"] == "你现在是谁"
    assert captured["cfg"]["max_tokens"] == 80


def test_niu_draw_request_uses_fast_image_path():
    orig = {"type": 1, "content": "牛来 画一个苹果", "is_self": True, "sender": "me", "local_id": 1}
    persona = {"name": "黎深", "persona": "你是黎深。"}
    with patch.object(bot, "_niu_draw", return_value="") as draw, \
         patch.object(bot.personalization, "maybe_switch", return_value=None):
        out = bot._ai_reply(persona, "wxid_friend", orig, [orig], rules={}, batch_msgs=[orig])
    assert out == ""
    assert draw.called
    assert "苹果" in draw.call_args.args[0]


def test_niu_settle_is_immediate():
    batch = [{"type": 1, "content": "牛来 你现在是谁", "is_self": True}]
    assert bot._settle_for(batch) == 0.0


def test_niu_draw_not_blocked_as_recent_duplicate():
    src = open("core/reply_policy.py", encoding="utf-8").read()
    assert "niu_batch" in src
    assert "niu_mode.is_bot_stamp" in src


def test_reply_policy_does_not_skip_self_niu():
    from core import reply_policy
    own = {"local_id": 1, "type": 1, "is_self": True, "sender": "account-A",
           "content": "牛来 你现在是谁"}
    d = reply_policy.decide([own], [own], "account-A", chat="g@chatroom")
    assert d.action == "reply"


def test_self_window_niu_is_enqueued_same_chat():
    src = open("core/bot.py", encoding="utf-8").read()
    assert "跳过自己的聊天窗口" not in src
    from core import sender
    with patch("config.wxid", return_value="wxid_vn2w1t3doieu22"):
        assert sender.is_self_chat("wxid_vn2w1t3doieu22")
        assert sender.search_key("wxid_vn2w1t3doieu22", "🐮🐮🌱besos") == (
            "🐮🐮🌱besos", "🐮🐮🌱besos")
        assert not sender.is_self_chat("wxid_pp1fsf398lxh21")


def test_niu_skipped_when_battle_on():
    src = open("core/bot.py", encoding="utf-8").read()
    assert "niu_mode.allowed(m, chat, watch_set)" in src
    assert "not battle_mode.is_on(chat)" in src


def test_allowed_watch_or_self_only():
    msg = {"type": 1, "content": "牛来 你现在是谁", "is_self": False}
    assert niu_mode.allowed(msg, "g@chatroom", {"g@chatroom"}) is True
    assert niu_mode.allowed(msg, "other@chatroom", {"g@chatroom"}) is False
    mine = dict(msg, is_self=True)
    assert niu_mode.allowed(mine, "other@chatroom", {"g@chatroom"}) is True
    assert niu_mode.allowed({"type": 1, "content": "你好", "is_self": True}, "g@chatroom", {"g@chatroom"}) is False


def test_wants_image_covers_draw_an_animal():
    from core import bot
    assert bot._wants_image("画一只戴着墨镜的小龙虾")
    assert bot._wants_image("画一个黎深")
    assert not bot._wants_image("中午吃什么")


def test_stamp_prefix():
    assert niu_mode.stamp("黎深") == "[牛牛模式]黎深"
    assert niu_mode.stamp("[牛牛模式]已经有了") == "[牛牛模式]已经有了"
    assert niu_mode.is_bot_stamp({"content": "[牛牛模式]在画了。"}) is True
    assert niu_mode.is_bot_stamp({"content": "牛来 画一个"}) is False


def test_self_niu_batch_is_kept():
    from core import reply_context
    raw = [{"content": "牛来 你现在是谁", "is_self": True, "sender": "me", "type": 1, "local_id": 1}]
    niu = [niu_mode.inbound(cm) for cm in raw if niu_mode.is_trigger(cm)]
    batch = reply_context.unique(niu)
    assert len(batch) == 1
    assert batch[0]["content"] == "你现在是谁"


def test_inbound_treats_self_as_ask():
    m = niu_mode.inbound({"content": "牛来 你现在是谁", "is_self": True, "sender": "wxid_me"})
    assert m["content"] == "你现在是谁"
    assert m["is_self"] is False
    assert m["_niu"] is True
    from core import reply_context
    assert reply_context.is_self(m, "wxid_me") is False
    turns, ask, batch = reply_context.build(
        [m], [m], account="wxid_me", is_group=True,
        render=lambda cm: cm.get("content") or "", name=lambda s: s or "",
        timestamp=lambda cm: "now", scheduled=lambda cm: False)
    assert batch and batch[0]["content"] == "你现在是谁"
    assert "你现在是谁" in ask
