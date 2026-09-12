"""离线单元测试：验证记忆的作用域/状态/时间过滤、按需检索、去重、上下文时间标注。

只测【可独立验证】的逻辑(过滤/时间/装配)。不调用付费模型、不发真实消息。
mock 出来的"回复"只能证明管线行为，不能证明真实自然度——自然度改善标【待验证】。

跑： python3 -m pytest tests/test_memory_ctx.py -q
或   python3 tests/test_memory_ctx.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402


def _fresh_account():
    d = tempfile.mkdtemp(prefix="wxtest_")
    # 把 account_dir 直接指向临时目录，隔离真实账号数据
    config.ACCOUNTS_DIR = d
    config.WORK_DIR = os.path.join(d, 'work')
    config.wxid = lambda: 'offline-test'
    os.makedirs(os.path.join(d, 'offline-test'), exist_ok=True)
    config.account_dir = lambda: os.path.join(d, 'offline-test')
    return d


NOW = 1_760_000_000  # 固定"现在"，让相对时间可断言


def _mk_profile(mem, wxid, facts):
    p = mem.load_profile(wxid)
    p["name"] = "测试对象"
    p["facts"] = facts
    mem.save_profile(p)


def run():
    acc = _fresh_account()  # noqa: F841
    from core import memory as mem

    passed, failed = 0, []

    def check(name, cond):
        nonlocal passed
        if cond:
            passed += 1
        else:
            failed.append(name)

    wid = "wxid_test"

    # ---- 1. 无价值/系统通知不入选 ----
    _mk_profile(mem, wid, [
        {"id": "j1", "text": "对方已通过好友验证", "type": "event",
         "status": "active", "scope": "chat:" + wid, "recorded_ts": NOW - 10},
        {"id": "p1", "text": "喜欢吃川菜", "type": "preference",
         "status": "active", "scope": "chat:" + wid, "recorded_ts": NOW - 86400},
    ])
    got = mem.select_memories(wid, "今晚吃川菜好不好", scope="chat:" + wid, now=NOW)
    check("junk-not-selected", all("好友验证" not in m["text"] for m in got))
    check("relevant-selected", any("川菜" in m["text"] for m in got))

    # ---- 2. 只有无价值事实时返回空(不拿 summary 兜底) ----
    _mk_profile(mem, wid, [
        {"id": "j2", "text": "已通过好友验证", "type": "event",
         "status": "active", "scope": "chat:" + wid, "recorded_ts": NOW},
    ])
    p = mem.load_profile(wid)
    p["summary"] = "一个老朋友"
    mem.save_profile(p)
    got = mem.select_memories(wid, "随便聊聊天气", scope="chat:" + wid, now=NOW)
    check("junk-only-empty", got == [])

    # ---- 3. 与当前话题无关 → 不选(允许空) ----
    _mk_profile(mem, wid, [
        {"id": "a1", "text": "在做一个航天项目", "type": "plan",
         "status": "active", "scope": "chat:" + wid, "recorded_ts": NOW},
    ])
    got = mem.select_memories(wid, "今天天气真好", scope="chat:" + wid, now=NOW)
    check("irrelevant-empty", got == [])

    # ---- 4. 作用域隔离：私聊记忆不串到群 ----
    _mk_profile(mem, wid, [
        {"id": "s1", "text": "喜欢喝手冲咖啡", "type": "preference",
         "status": "active", "scope": "chat:" + wid, "recorded_ts": NOW},
    ])
    got = mem.select_memories(wid, "喝咖啡吗", scope="group:room1", now=NOW)
    check("scope-isolation", got == [])
    got2 = mem.select_memories(wid, "喝咖啡吗", scope="chat:" + wid, now=NOW)
    check("scope-match", any("咖啡" in m["text"] for m in got2))

    # ---- 5. 情绪类过期不再入选 ----
    _mk_profile(mem, wid, [
        {"id": "e1", "text": "今天心情很累", "type": "emotion", "status": "active",
         "scope": "chat:" + wid, "recorded_ts": NOW - 10 * 86400,
         "expires_ts": NOW - 5 * 86400},
    ])
    got = mem.select_memories(wid, "你还累吗", scope="chat:" + wid, now=NOW)
    check("emotion-expired", got == [])

    # ---- 6. 被取消/取代的事实不主动用 ----
    _mk_profile(mem, wid, [
        {"id": "c1", "text": "周末计划去杭州", "type": "plan", "status": "cancelled",
         "scope": "chat:" + wid, "recorded_ts": NOW - 86400},
        {"id": "c2", "text": "周末计划去苏州", "type": "plan", "status": "active",
         "scope": "chat:" + wid, "recorded_ts": NOW},
    ])
    got = mem.select_memories(wid, "周末计划去哪", scope="chat:" + wid, now=NOW)
    check("cancelled-not-used", all(m["id"] != "c1" for m in got))
    check("active-plan-used", any(m["id"] == "c2" for m in got))

    # ---- 7. 预算上限 ----
    facts = [{"id": f"m{i}", "text": f"喜欢运动项目{i}篮球", "type": "preference",
              "status": "active", "scope": "chat:" + wid, "recorded_ts": NOW - i}
             for i in range(10)]
    _mk_profile(mem, wid, facts)
    got = mem.select_memories(wid, "篮球", scope="chat:" + wid, budget=3, now=NOW)
    check("budget-cap", len(got) <= 3)

    # ---- 8. 更正：mark_superseded 保留历史、优先新值 ----
    _mk_profile(mem, wid, [
        {"id": "o1", "text": "住在北京", "type": "identity", "status": "active",
         "scope": "chat:" + wid, "recorded_ts": NOW - 100 * 86400},
    ])
    ok = mem.mark_superseded(wid, "o1", "住在上海", "identity", source_msg_id="msg123")
    check("supersede-ok", ok)
    p = mem.load_profile(wid)
    old = next(f for f in p["facts"] if f["id"] == "o1")
    check("old-superseded", old["status"] == "superseded")
    new = next(f for f in p["facts"] if f.get("text") == "住在上海")
    check("new-has-source", new.get("source_msg_id") == "msg123")
    got = mem.select_memories(wid, "你住在哪个城市", scope="chat:" + wid, now=NOW)
    check("prefers-new", any("上海" in m["text"] for m in got)
          and all("北京" not in m["text"] for m in got))

    # ---- 9. 旧 {fact,ts} 结构向后兼容(ts 当记录时间,不当事件时间) ----
    p = mem.load_profile("wxid_legacy")
    p["facts"] = [{"fact": "老数据事实喜欢钓鱼", "ts": NOW - 200 * 86400}]
    mem.save_profile(p)
    p2 = mem.load_profile("wxid_legacy")
    f = p2["facts"][0]
    check("legacy-migrated", f.get("text") == "老数据事实喜欢钓鱼"
          and f.get("recorded_ts") == NOW - 200 * 86400
          and f.get("event_ts") is None
          and f.get("status") == "active")

    # ---- 10. recently_mentioned 去重(近似) ----
    check("recent-mention-hit",
          mem.recently_mentioned("喜欢吃川菜", ["我记得你喜欢吃川菜"]))
    check("recent-mention-miss",
          not mem.recently_mentioned("在做航天项目", ["今天天气不错"]))

    # ---- 11. 时间标注：记录时间标注写明非事件时间 ----
    lbl = mem._rel_time(None, NOW - 40 * 86400, NOW)
    check("record-time-labeled", "记录于" in lbl)
    lbl2 = mem._rel_time(NOW - 40 * 86400, NOW - 100, NOW)
    check("event-time-preferred", "记录于" not in lbl2)

    # ---- 12. bot 上下文装配：批内按 ID 去重 + 相对时间标注 ----
    from core import bot
    tl = bot._msg_time_label(NOW - 30, NOW)
    check("ctx-just-now", tl == "刚刚")
    tl2 = bot._msg_time_label(NOW - 3 * 86400, NOW)
    check("ctx-days-ago", "天前" in tl2)

    # ---- 13. 话题簇：无词面重合但同话题(不吃辣↔晚饭推荐)能召回 ----
    _mk_profile(mem, wid, [
        {"id": "t1", "text": "不吃辣", "type": "preference", "status": "active",
         "scope": "chat:" + wid, "recorded_ts": NOW},
        {"id": "t2", "text": "在做一个航天项目", "type": "plan", "status": "active",
         "scope": "chat:" + wid, "recorded_ts": NOW},
    ])
    got = mem.select_memories(wid, "晚饭有什么推荐", scope="chat:" + wid, now=NOW)
    check("topic-cluster-recall", any(m["id"] == "t1" for m in got))
    check("topic-cluster-precise", all(m["id"] != "t2" for m in got))
    # "那个餐厅"依赖 recent_topic 里带出的近期提及
    got2 = mem.select_memories(wid, "那个还去吗", recent_topic="上次说的那家川菜餐厅",
                               scope="chat:" + wid, now=NOW)
    check("anaphora-via-recent-topic", any(m["id"] == "t1" for m in got2))
    # 词典外语义仍可能漏——不声称覆盖所有语义(此处只断言机制存在,不断言全覆盖)

    # ---- 14. 更正/取消接入抽取链(mock LLM, 不调付费模型) ----
    from core import llm as _llm
    _mk_profile(mem, wid, [
        {"id": "pl1", "text": "周六要去看电影", "type": "plan", "status": "active",
         "scope": "chat:" + wid, "recorded_ts": NOW - 86400},
        {"id": "pl2", "text": "下月要去出差", "type": "plan", "status": "active",
         "scope": "chat:" + wid, "recorded_ts": NOW - 86400},
    ])
    orig_chat, orig_avail = _llm.chat, _llm.available
    _llm.available = lambda: True
    msgs = [{"sender": wid, "sender_name": "测试对象", "is_self": False,
             "content": c, "server_id": 900 + i} for i, c in enumerate(
        ["跟你说下", "周六的电影不去了", "临时有事"])]
    # (a) LLM 明确对应到 pl1 → 取消
    _llm.chat = lambda *a, **k: ('{"facts":{},"updates":{"测试对象":'
                                 '[{"op":"cancel","id":"pl1"}]}}')
    mem.extract_from_messages(msgs, chat_scope="chat:" + wid)
    p = mem.load_profile(wid)
    st = {f["id"]: f["status"] for f in p["facts"]}
    check("cancel-wired", st.get("pl1") == "cancelled")
    check("cancel-precise", st.get("pl2") == "active")
    src = next(f for f in p["facts"] if f["id"] == "pl1").get("source_msg_id")
    check("cancel-has-source", src is not None)
    # (b) LLM 输出不存在的 ID → 拒绝(保守,不乱取消)
    _llm.chat = lambda *a, **k: ('{"facts":{},"updates":{"测试对象":'
                                 '[{"op":"cancel","id":"f_notexist"}]}}')
    mem.extract_from_messages(msgs, chat_scope="chat:" + wid)
    p = mem.load_profile(wid)
    check("bogus-id-rejected",
          all(f["status"] == ("cancelled" if f["id"] == "pl1" else "active")
              for f in p["facts"] if f["id"] in ("pl1", "pl2")))
    # (c) supersede 带来源
    _llm.chat = lambda *a, **k: ('{"facts":{},"updates":{"测试对象":'
                                 '[{"op":"supersede","id":"pl2","new_text":"出差改到年底"}]}}')
    mem.extract_from_messages(msgs, chat_scope="chat:" + wid)
    p = mem.load_profile(wid)
    st = {f["id"]: f for f in p["facts"]}
    check("supersede-wired", st["pl2"]["status"] == "superseded"
          and any(f.get("text") == "出差改到年底" and f.get("source_msg_id")
                  for f in p["facts"]))
    _llm.chat, _llm.available = orig_chat, orig_avail

    # ---- 15. 工具授权：模型传别的 chat 被服务端拒绝;画像按当前会话作用域过滤 ----
    from core import tools as tl_mod
    r = tl_mod.run("search_history", {"query": "x", "chat": "wxid_other"},
                   {"chat": "wxid_auth"})
    check("history-chat-forced", "只允许" in r)
    _mk_profile(mem, wid, [
        {"id": "pv1", "text": "私聊里说过的秘密爱好是钓鱼", "type": "preference",
         "status": "active", "scope": "chat:" + wid, "recorded_ts": NOW},
    ])
    p = mem.load_profile(wid)
    p["summary"] = "私聊聚合摘要"
    mem.save_profile(p)
    import core.tools as _tools
    from core import read_access
    config.account_key = lambda: "wxid_test_account"
    def auth(chat):
        return read_access.Access("wxid_test_account", os.path.realpath(config.account_dir()),
                                  chat, frozenset({wid}), ((wid, wid),))
    out_group = _tools.get_member_profile(wid, access=auth("room9@chatroom"))
    check("profile-scope-enforced", "钓鱼" not in out_group and "私聊聚合摘要" not in out_group)
    out_priv = _tools.get_member_profile(wid, access=auth(wid))
    check("profile-own-chat-ok", "钓鱼" in out_priv)

    # ---- 16. 诊断脱敏：默认不落正文,两级开关 ----
    os.environ["WXBOT_DIAG"] = "1"
    os.environ.pop("WXBOT_DIAG_TEXT", None)
    dlog = os.path.join(config.account_dir(), "diag.log")
    if os.path.exists(dlog):
        os.remove(dlog)
    bot._diag("wxid_auth", "select_memories",
              {"qlen": 9, "n": 1, "picked": ["f_1"], "query_text": "机密内容别落盘"})
    content = open(dlog, encoding="utf-8").read() if os.path.exists(dlog) else ""
    check("diag-no-text-default", "机密内容" not in content and '"qlen": 9' in content)
    os.environ["WXBOT_DIAG_TEXT"] = "1"
    bot._diag("wxid_auth", "select_memories", {"query_text": "显式开关后可落"})
    content = open(dlog, encoding="utf-8").read()
    check("diag-text-explicit", "显式开关后可落" in content)
    os.environ.pop("WXBOT_DIAG", None)
    os.environ.pop("WXBOT_DIAG_TEXT", None)

    # ---- 17. recently_mentioned 未接入选择过滤:刚注入/刚用过不会丢偏好 ----
    got = mem.select_memories(wid, "晚饭吃什么", scope="chat:" + wid, now=NOW)
    got_again = mem.select_memories(wid, "晚饭吃什么", scope="chat:" + wid, now=NOW)
    check("no-cooldown-loss", [m["id"] for m in got] == [m["id"] for m in got_again])

    print(f"\n通过 {passed} / {passed + len(failed)}")
    if failed:
        print("失败用例：", ", ".join(failed))
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(run())
