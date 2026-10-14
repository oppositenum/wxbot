from core import context_reset, bot, reply_context


def test_filter_history_drops_messages_at_or_before_reset(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "account_dir", lambda: str(tmp_path))
    context_reset.clear("chat-A", after_id=10)
    rows = [{"local_id": i, "content": str(i)} for i in (8, 10, 11, 12)]
    got = context_reset.filter_history("chat-A", rows)
    assert [m["local_id"] for m in got] == [11, 12]


def test_reset_chat_context_uses_latest_local_id(monkeypatch):
    monkeypatch.setattr("core.messages.get_messages", lambda chat, limit=1: [{"local_id": 42}])
    monkeypatch.setattr(bot, "_pending", {"chat-A": {"msgs": [1]}}, raising=False)
    monkeypatch.setattr(bot, "save_pending", lambda: None)
    info = bot.reset_chat_context("chat-A")
    assert info["after_id"] == 42
    assert "chat-A" not in bot._pending


def test_ai_reply_history_is_filtered():
    src = open("core/bot.py", encoding="utf-8").read()
    assert "context_reset.filter_history" in src
    assert "reset_chat_context" in src
