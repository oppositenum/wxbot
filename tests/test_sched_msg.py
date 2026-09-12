"""离线测试：定时任务消息的标识与排除机制。

不发真实微信消息、不调付费模型(全部 mock)。
跑： python3 tests/test_sched_msg.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="wxsched_")
config.ACCOUNTS_DIR = _TMP
config.WORK_DIR = os.path.join(_TMP, 'work')
config.wxid = lambda: 'offline-test'
os.makedirs(os.path.join(_TMP, 'offline-test'), exist_ok=True)
config.account_dir = lambda: os.path.join(_TMP, 'offline-test')

from core import schedule as sch  # noqa: E402
from core import llm as _llm  # noqa: E402

passed, failed = 0, []


def check(name, cond):
    global passed
    if cond:
        passed += 1
    else:
        failed.append(name)


NOW = int(time.time())
CHAT = "wxid_target01"


def run():
    # ---- 1. 前缀只加一次(公共发送路径 fire 里加) ----
    sent = []

    class _FakeSender:
        @staticmethod
        def preflight(*a, **kw):
            return None  # Explicit synthetic transport capability.

        @staticmethod
        def send_text(disp, text, chat_username=None, **kw):
            sent.append((chat_username, text))
            return {"ok": True, "verified": True, "status": "confirmed", "reason": "test_receipt"}

        @staticmethod
        def send_at(disp, uname, mw, md, text, **kw):
            sent.append((uname, text))
            return {"ok": True, "status": "confirmed", "reason": "test_receipt"}
    sys.modules["core.sender"] = _FakeSender()

    t1 = {"id": 101, "title": "接娃", "prompt": "要去接大儿放学了",
          "target_username": CHAT, "target_display": "测试目标"}
    sch.fire(t1)
    check("prefix-added", sent[-1][1] == "【定时提醒】要去接大儿放学了")
    t2 = dict(t1, id=102, prompt="【定时提醒】已带前缀的正文")
    sch.fire(t2)
    check("prefix-once", sent[-1][1] == "【定时提醒】已带前缀的正文"
          and not sent[-1][1].startswith("【定时提醒】【定时提醒】"))

    # ---- 2. 执行记录已落盘(source=scheduled_task, task_id, chat) ----
    fires = json.load(open(os.path.join(config.account_dir(), "schedule_fires.json"), encoding="utf-8"))
    check("fire-recorded", len(fires) == 2
          and fires[0]["source"] == "scheduled_task"
          and fires[0]["task_id"] == 101 and fires[0]["chat"] == CHAT)

    # ---- 3. is_scheduled_msg 识别 ----
    rec_text = "【定时提醒】要去接大儿放学了"
    # (a) 自己发的+有记录(时间窗内) → 是
    check("self-with-record", sch.is_scheduled_msg(
        {"is_self": True, "content": rec_text, "create_time": NOW + 10}, CHAT))
    # (b) 自己发的+带前缀+无记录(比如老会话) → 兜底也算
    check("self-prefix-fallback", sch.is_scheduled_msg(
        {"is_self": True, "content": "【定时提醒】旧提醒无记录", "create_time": NOW - 99999}, CHAT))
    # (c) 【用户】发的带前缀 → 绝不过滤(问"【定时提醒】是什么意思"必须正常处理)
    check("user-prefix-not-filtered", not sch.is_scheduled_msg(
        {"is_self": False, "content": "【定时提醒】是什么意思?", "create_time": NOW}, CHAT))
    # (d) 旧的无前缀提醒且无记录 → 不凭正文猜,不过滤
    check("legacy-noprefix-kept", not sch.is_scheduled_msg(
        {"is_self": True, "content": "要去接大儿放学了", "create_time": NOW - 86400 * 30}, CHAT))
    # (e) 记录匹配但时间窗外(同文本很久以后自己手打) → 靠前缀判,无前缀不过滤
    check("window-limited", not sch.is_scheduled_msg(
        {"is_self": True, "content": "要去接大儿放学了", "create_time": NOW + 7200}, CHAT))

    # ---- 4. 上下文装配排除 + 同批正常消息不丢 ----
    from core import bot, agent, distill
    captured = {}
    _llm.available = lambda: True
    _llm.chat = lambda system, msgs, cfg=None: (captured.update(
        system=system, user="\n".join(m["content"] for m in msgs)) or "好的")
    _llm.load_cfg = lambda: {}
    bot._sender_name = lambda w: "测试对象"
    bot._enrich_media = lambda c, m: (m.get("content") or "")
    agent.agent_config = lambda r=None: {"enabled": False, "tools": []}
    distill.pick_samples = lambda *a, **k: []
    persona = {"name": "P", "persona": "你是助手", "samples": []}
    ctx_msgs = [
        {"local_id": 1, "server_id": 1, "is_self": True, "content": rec_text,
         "create_time": NOW + 10, "sender": "me", "type": 1},          # 定时提醒→应被排除
        {"local_id": 2, "server_id": 2, "is_self": False, "content": "收到,知道了",
         "create_time": NOW + 20, "sender": CHAT, "type": 1},          # 正常
        {"local_id": 3, "server_id": 3, "is_self": False, "content": "【定时提醒】是什么意思?",
         "create_time": NOW + 30, "sender": CHAT, "type": 1},          # 用户引用前缀→保留
    ]
    msg = ctx_msgs[-1]
    out = bot._ai_reply(persona, CHAT, msg, ctx_msgs, rules={})
    check("reply-not-empty", out == "好的")
    check("ctx-excludes-sched", rec_text not in captured["user"])
    check("ctx-keeps-batch", "收到,知道了" in captured["user"]
          and "【定时提醒】是什么意思?" in captured["user"])

    # ---- 5. 触发端：定时消息不进规则/待回批(谓词) + 不路由到定时的普通问句 ----
    check("no-trigger-selfsched", sch.is_scheduled_msg(
        {"is_self": True, "content": rec_text, "create_time": NOW + 5}, CHAT))
    check("prefix-question-normal-chat",
          not sch.looks_schedule("【定时提醒】是什么意思?")
          and not sch.looks_reminder_response("【定时提醒】是什么意思?"))

    # ---- 6. search_history 排除定时消息、保留用户同前缀消息 ----
    from core import messages as msgs_mod, tools
    # Exercise the actual pinned read-only SQL reader against a temporary message DB.
    import sqlite3, hashlib
    from core import read_access
    config.account_key = lambda: "wxid_test_account"
    os.makedirs(os.path.join(config.account_dir(), "decrypted"), exist_ok=True)
    con = sqlite3.connect(os.path.join(config.account_dir(), "decrypted", "message.db"))
    con.execute("CREATE TABLE Name2Id(user_name TEXT)")
    con.executemany("INSERT INTO Name2Id VALUES(?)", [("wxid_test_account",), (CHAT,)])
    table = "Msg_" + hashlib.md5(CHAT.encode()).hexdigest()
    con.execute("CREATE TABLE " + table + " (local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content TEXT)")
    con.executemany("INSERT INTO " + table + " VALUES(?,?,?,?)", [
        (1, 1, NOW + 10, rec_text), (1, 2, NOW + 20, "我们聊过定时提醒的事")])
    con.commit(); con.close()
    access = read_access.Access("wxid_test_account", os.path.realpath(config.account_dir()), CHAT,
                               frozenset({CHAT}), ((CHAT, CHAT),))
    r = tools.search_history("定时提醒", CHAT, access=access)
    check("history-excludes-sched", "接大儿放学" not in r)
    check("history-keeps-user", "聊过定时提醒的事" in r)

    # ---- 7. 明确查询提醒记录：recent_fires / handle_nl(action=fired) ----
    rf = sch.recent_fires(chat=CHAT)
    check("recent-fires-query", len(rf) == 2 and rf[0]["title"] == "接娃")
    _llm.chat = lambda *a, **k: '{"action":"fired"}'
    r = sch.handle_nl("刚才提醒了什么", {"chat_username": CHAT, "requester_wxid": "u1"})
    check("fired-answerable", r["ok"] and "接大儿放学" in r["message"])
    # 会话边界：别的会话查不到本会话的提醒记录
    r2 = sch.handle_nl("刚才提醒了什么", {"chat_username": "wxid_other", "requester_wxid": "u1"})
    check("fired-chat-scoped", "没有触发过" in r2["message"])
    check("manage-regex-routes", sch.looks_schedule("刚才提醒了什么"))

    # ---- 8. 对提醒的回应：定位/取消/改时间/澄清 ----
    check("resp-detected", sch.looks_reminder_response("不用提醒了")
          and sch.looks_reminder_response("改成六点")
          and not sch.looks_reminder_response("我改天再说")
          and not sch.looks_reminder_response("今天好累"))
    # 建两条真实任务(临时目录)
    sch.resolve_target = lambda name: (CHAT, "测试目标", False)
    ta, e1 = sch.add_task(title="接娃", target="测试目标", prompt="要去接大儿放学了",
                          cron="0 18 * * *", creator_wxid="u1")
    tb, e2 = sch.add_task(title="吃药", target="测试目标", prompt="记得吃药",
                          cron="0 9 * * *", creator_wxid="u1")
    check("tasks-created", not e1 and not e2)
    # (a) "不用提醒了"→LLM结合最近提醒上下文取消对应任务;确认 extra 传了最近提醒
    seen_prompt = {}

    def _mock_cancel(system, msgs2, cfg=None):
        seen_prompt["u"] = msgs2[0]["content"]
        return '{"action":"cancel","cancel_match":"接娃"}'
    _llm.chat = _mock_cancel
    r = sch.handle_nl("不用提醒了", {"chat_username": CHAT, "requester_wxid": "u1"})
    check("cancel-by-response", r["ok"] and "已取消 1" in r["message"])
    check("recent-fires-in-prompt", "最近触发的提醒" in seen_prompt["u"]
          and "接娃" in seen_prompt["u"])
    check("other-task-intact", any(t["title"] == "吃药" for t in sch.load_tasks()))
    # (b) "改成六点"→update 唯一匹配任务
    _llm.chat = lambda *a, **k: ('{"action":"update","cancel_match":"吃药",'
                                 '"cron":"0 6 * * *"}')
    r = sch.handle_nl("吃药提醒改成六点", {"chat_username": CHAT, "requester_wxid": "u1"})
    check("update-by-response", r["ok"] and "0 6" in json.dumps(sch.load_tasks(),
                                                                ensure_ascii=False))
    # (c) 指代不明→ask 澄清,不乱动
    _llm.chat = lambda *a, **k: '{"action":"ask","ask":"你说的是哪条提醒?"}'
    n_before = len(sch.load_tasks())
    r = sch.handle_nl("那个不要了", {"chat_username": CHAT, "requester_wxid": "u1"})
    check("ambiguous-asks", r["action"] == "ask" and "哪条" in r["message"]
          and len(sch.load_tasks()) == n_before)
    # (d) update 匹配不到→反问而不是乱改
    _llm.chat = lambda *a, **k: ('{"action":"update","cancel_match":"不存在的任务",'
                                 '"cron":"0 6 * * *"}')
    r = sch.handle_nl("那个改成六点", {"chat_username": CHAT, "requester_wxid": "u1"})
    check("update-nomatch-asks", not r["ok"] and "哪条" in r["message"])

    # ---- 9. 记忆抽取不吃定时消息(is_self 全跳过) ----
    from core import memory as mem
    calls = []
    _llm.chat = lambda system, m2, cfg=None: (calls.append(m2[0]["content"]) or
                                              '{"facts":{},"updates":{}}')
    mem.extract_from_messages([
        {"is_self": True, "content": rec_text, "create_time": NOW, "sender": "me",
         "sender_name": "我"},
        {"is_self": False, "content": "今天上班好累啊", "sender": CHAT,
         "sender_name": "测试对象"},
        {"is_self": False, "content": "晚上想吃火锅", "sender": CHAT,
         "sender_name": "测试对象"},
        {"is_self": False, "content": "周末打算去爬山", "sender": CHAT,
         "sender_name": "测试对象"},
    ], chat_scope="chat:" + CHAT)
    check("memory-excludes-sched", calls and "接大儿放学" not in calls[0]
          and "火锅" in calls[0])

    print(f"\n通过 {passed} / {passed + len(failed)}")
    if failed:
        print("失败：", ", ".join(failed))
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(run())
