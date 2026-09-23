"""战斗模式：短句连发格式锁、连发间隔、账号目录持久化。"""
import json
from pathlib import Path

from core import battle_mode, bot


def test_system_text_forces_short_bursts():
    text = battle_mode.system_text()
    assert "[[NEXT]]" in text
    assert "25" in text
    assert "禁止长篇" in text


def test_default_persona_is_short_and_sharp():
    p = battle_mode.DEFAULT_PERSONA
    assert "短促" in p or "极短" in p
    assert "长篇" in p or "议论文" in p


def test_battle_part_gap_is_short():
    assert bot._part_gap(1) == 0.0
    assert bot._part_gap(2) >= 8.0
    assert 1.0 <= bot._part_gap(2, rapid=True) <= 3.0


def test_split_next_keeps_burst_bubbles():
    parts = bot._reply_parts("就这？\n[[NEXT]]\n逻辑呢\n[[NEXT]]\n再说一遍试试")
    assert parts == ["就这？", "逻辑呢", "再说一遍试试"]


def test_battle_chat_cfg_overrides_grok_model(tmp_path, monkeypatch):
    import config
    from core import llm
    monkeypatch.setattr(config, "account_dir", lambda: str(tmp_path))
    monkeypatch.setattr(config, "WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(llm, "load_cfg", lambda: {
        "provider": "gpt", "grok": {"model": "grok-4.5", "api_key": "k", "base_url": "https://x"}
    })
    assert battle_mode.chat_cfg() is None
    battle_mode.set_grok_route("grok-4.20-0309-non-reasoning", "none")
    cfg = battle_mode.chat_cfg()
    assert cfg["provider"] == "grok"
    assert cfg["grok"]["model"] == "grok-4.20-0309-non-reasoning"
    assert cfg["grok_reasoning_effort"] == "none"
    assert cfg["no_gpt_fallback"] is True


def test_persona_persists_in_account_dir(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "account_dir", lambda: str(tmp_path))
    monkeypatch.setattr(config, "WORK_DIR", str(tmp_path / "work"))
    battle_mode.set_persona("短句怼人")
    battle_mode.enable("room@chatroom")
    saved = json.loads((tmp_path / "battle_mode.json").read_text(encoding="utf-8"))
    assert saved["persona"] == "短句怼人"
    assert saved["chats"] == ["room@chatroom"]
    assert not (tmp_path / "work" / "battle_mode.json").exists()


def test_group_auto_matches_self_when_include_self():
    msg = {"is_self": True, "at_me": False, "quote_me": False, "content": "你现在是谁", "sender": "me"}
    assert bot.match_rule({"match": {"type": "auto"}}, msg, True, chat="g@chatroom",
                          rules={"include_self": True}) == {}
    assert bot.match_rule({"match": {"type": "auto"}}, msg, True, chat="g@chatroom",
                          rules={"include_self": False}) is None


def test_priority_senders_match_group_id_or_name():
    from unittest.mock import patch
    rules = {"group_priority_senders": {"Limit": ["pian1225"]}}
    contacts = [{"username": "wxid_wife", "alias": "pian1225", "remark": "老婆",
                 "name": "老婆", "nick_name": "蜜蜜"}]
    groups = [{"username": "49522251292@chatroom", "name": "Limit"}]
    with patch("core.contacts.list_groups", return_value=groups), \
         patch("core.contacts.list_contacts", return_value=contacts):
        got = bot._priority_senders(rules, "49522251292@chatroom")
    assert got == {"wxid_wife"}


def test_migrates_legacy_work_file(tmp_path, monkeypatch):
    import config
    acct = tmp_path / "acct"
    work = tmp_path / "work"
    acct.mkdir()
    work.mkdir()
    (work / "battle_mode.json").write_text(json.dumps({
        "wxid_me": {"chats": ["g@chatroom"], "persona": "旧人设留下"}
    }), encoding="utf-8")
    monkeypatch.setattr(config, "account_dir", lambda: str(acct))
    monkeypatch.setattr(config, "account_key", lambda: "wxid_me")
    monkeypatch.setattr(config, "WORK_DIR", str(work))
    assert battle_mode.get_persona() == "旧人设留下"
    assert battle_mode.active_chats() == ["g@chatroom"]
    assert json.loads((acct / "battle_mode.json").read_text(encoding="utf-8"))["persona"] == "旧人设留下"
