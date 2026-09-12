"""离线测试：纯图片触发回复(延长去抖) + 对方沉默后的主动跟进(1-2次,不扰)。
全 mock,不发真实消息、不调付费模型。 跑: python3 tests/test_media_nudge.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="wxmn_")
config.ACCOUNTS_DIR = _TMP
config.WORK_DIR = os.path.join(_TMP, 'work')
config.wxid = lambda: 'offline-test'
os.makedirs(os.path.join(_TMP, 'offline-test'), exist_ok=True)
config.account_dir = lambda: os.path.join(_TMP, 'offline-test')

from core import bot, llm, sender, distill, messages, schedule, decrypt, agent, conversation_state  # noqa: E402

passed, failed = 0, []


def check(name, cond):
    global passed
    if cond:
        passed += 1
    else:
        failed.append(name)


CHAT = "wxid_friend01"
# 固定"现在"=某天下午14:00(非夜间静默时段)
NOW = time.mktime((2026, 9, 9, 14, 0, 0, 0, 0, -1))

RULES = {"include_self": False, "watch": [CHAT], "poll_interval": 5,
         "rules": [{"name": "style-reply", "match": {"type": "auto"},
                    "action": {"type": "reply_ai", "persona": "P"}}]}

sent = []
llm.available = lambda: True
llm.chat = lambda system, msgs, cfg=None: "自动生成的回复"
llm.load_cfg = lambda: {}
sender.preflight = lambda *a, **kw: None  # Synthetic available transport; no real sends.
sender.send_text = lambda disp, text, chat_username=None, **kw: (
    sent.append((chat_username, text)) or {"ok": True, "status": "confirmed", "reason": "test_receipt"})
distill.load_persona = lambda slug: ({"name": "P", "persona": "你是助手",
                                      "samples": []} if slug else None)
distill.pick_samples = lambda *a, **k: []
agent.agent_config = lambda r=None: {"enabled": False, "tools": []}
bot.send_name_for = lambda u: "朋友"
bot._sender_name = lambda w: "朋友"
bot._enrich_media = lambda c, m: (m.get("content") or "")
bot._media_result = lambda c, m: bot.media_read.Result("success", "图片", "离线模拟图片描述")
decrypt.run = lambda force=False: None
schedule.is_scheduled_msg = lambda m, chat=None: bool(
    m.get("is_self") and (m.get("content") or "").startswith("【定时提醒】"))


conversation_state.latest = lambda chat, after=0: messages.get_messages(chat, limit=1000)

def run():
    # ---- 1. _settle_for：含文字批=普通去抖;纯媒体批=更长窗 ----
    check("settle-text", bot._settle_for([{"type": 3}, {"type": 1}]) == bot.SETTLE)
    check("settle-media", bot._settle_for([{"type": 3}]) == bot.MEDIA_SETTLE)
    check("settle-cfg", bot._settle_for([{"type": 3}], {"media_settle": 45}) == 45)

    # ---- 2. 纯图片进待回队列并在超时后回复 ----
    msg_store = {"msgs": [
        {"local_id": 1, "server_id": 1, "type": 1, "is_self": False,
         "content": "旧消息", "create_time": NOW - 9000, "sender": CHAT}]}
    messages.get_messages = lambda chat, limit=40: list(msg_store["msgs"])
    state = bot.load_state()
    bot.load_pending()
    bot._pending.clear()
    bot.run_once(RULES, state, log=lambda *_: None)   # 首见:只记指针
    msg_store["msgs"].append(
        {"local_id": 2, "server_id": 2, "type": 3, "is_self": False,
         "content": "[图片]", "create_time": NOW, "sender": CHAT})
    bot.run_once(RULES, state, log=lambda *_: None)
    check("image-queued", CHAT in bot._pending
          and bot._pending[CHAT]["msgs"][0]["type"] == 3)
    # 8s(普通SETTLE)后不该回(纯媒体要等MEDIA_SETTLE)
    bot._pending[CHAT]["last_seen"] = time.time() - (bot.SETTLE + 2)
    sent.clear()
    bot.run_once(RULES, state, log=lambda *_: None)
    check("media-waits-longer", CHAT in bot._pending and not sent)
    # 超过 MEDIA_SETTLE → 回复
    bot._pending[CHAT]["last_seen"] = time.time() - (bot.MEDIA_SETTLE + 2)
    bot.run_once(RULES, state, log=lambda *_: None)
    check("media-replied", sent and sent[-1][0] == CHAT and CHAT not in bot._pending)
    # 图+文字混批:普通 SETTLE 就回
    msg_store["msgs"].append(
        {"local_id": 3, "server_id": 3, "type": 3, "is_self": False,
         "content": "[图片]", "create_time": NOW + 60, "sender": CHAT})
    msg_store["msgs"].append(
        {"local_id": 4, "server_id": 4, "type": 1, "is_self": False,
         "content": "看这个", "create_time": NOW + 62, "sender": CHAT})
    bot.run_once(RULES, state, log=lambda *_: None)
    bot._pending[CHAT]["last_seen"] = time.time() - (bot.SETTLE + 1)
    sent.clear()
    bot.run_once(RULES, state, log=lambda *_: None)
    check("mixed-normal-settle", sent and CHAT not in bot._pending)

    # ---- 3. 主动跟进状态机 ----
    def mk(last_is_self, silence, extra_self=None):
        ms = [{"local_id": 10, "type": 1, "is_self": False, "content": "你在吗",
               "create_time": NOW - silence - 60, "sender": CHAT},
              {"local_id": 11, "type": 1, "is_self": last_is_self,
               "content": "我回你了" if last_is_self else "又发一条",
               "create_time": NOW - silence, "sender": "me" if last_is_self else CHAT}]
        if extra_self:
            ms.append(extra_self)
        return ms

    st = {}
    sent.clear()
    # (a) 最后一句是对方的→不跟进(等正常回复流程)
    check("no-nudge-when-their-turn",
          not bot._maybe_nudge(CHAT, mk(False, 3 * 3600), RULES, st,
                               lambda *_: None, now=NOW))
    # (b) 我方最后发言,静默1小时(<2h)→还不跟
    check("too-early", not bot._maybe_nudge(CHAT, mk(True, 3600), RULES, st,
                                            lambda *_: None, now=NOW))
    # (c) 静默3小时→第一次跟进
    check("nudge-1", bot._maybe_nudge(CHAT, mk(True, 3 * 3600), RULES, st,
                                      lambda *_: None, now=NOW) and len(sent) == 1)
    # (d) 刚跟进完再查→冷却(间隔<4h)不发第二次
    ms2 = mk(True, 3 * 3600, extra_self={
        "local_id": 12, "type": 1, "is_self": True, "content": "自动生成的回复",
        "create_time": NOW - 600, "sender": "me"})
    check("gap-respected", not bot._maybe_nudge(CHAT, ms2, RULES, st,
                                                lambda *_: None, now=NOW))
    # (e) 第一次跟进后又静默5小时→第二次(也是最后一次)
    ms3 = mk(True, 9 * 3600, extra_self={
        "local_id": 12, "type": 1, "is_self": True, "content": "自动生成的回复",
        "create_time": NOW - 5 * 3600, "sender": "me"})
    check("nudge-2", bot._maybe_nudge(CHAT, ms3, RULES, st,
                                      lambda *_: None, now=NOW) and len(sent) == 2)
    # (f) 已达2次上限→即使继续静默也不再发
    ms4 = mk(True, 20 * 3600, extra_self={
        "local_id": 13, "type": 1, "is_self": True, "content": "自动生成的回复",
        "create_time": NOW - 10 * 3600, "sender": "me"})
    check("max-2", not bot._maybe_nudge(CHAT, ms4, RULES, st,
                                        lambda *_: None, now=NOW) and len(sent) == 2)
    # (g) 对方终于说话(比之前更新的入站)→计数重置,下一轮静默(2.5h后)又可跟进
    ms5 = ms4 + [{"local_id": 14, "type": 1, "is_self": False, "content": "在忙",
                  "create_time": NOW - 1800, "sender": CHAT},
                 {"local_id": 15, "type": 1, "is_self": True, "content": "好",
                  "create_time": NOW - 1500, "sender": "me"}]
    check("reset-on-incoming", bot._maybe_nudge(CHAT, ms5, RULES, st,
                                                lambda *_: None,
                                                now=NOW + 2.5 * 3600)
          and len(sent) == 3)
    # (h) 夜间(凌晨2点)不打扰
    night = time.mktime((2026, 9, 10, 2, 0, 0, 0, 0, -1))
    st2 = {}
    check("quiet-hours", not bot._maybe_nudge(
        CHAT, mk(True, 3 * 3600), RULES, st2, lambda *_: None, now=night))
    # (i) 最后一条是定时提醒(排除后我方无发言/对方最后)→不因提醒而跟进
    ms6 = [{"local_id": 20, "type": 1, "is_self": False, "content": "好的知道了",
            "create_time": NOW - 3 * 3600, "sender": CHAT},
           {"local_id": 21, "type": 1, "is_self": True,
            "content": "【定时提醒】要去接大儿放学了",
            "create_time": NOW - 600, "sender": "me"}]
    st3 = {}
    check("sched-not-counted", not bot._maybe_nudge(
        CHAT, ms6, RULES, st3, lambda *_: None, now=NOW))
    # (j) 配置可关
    st4 = {}
    check("cfg-disable", not bot._maybe_nudge(
        CHAT, mk(True, 3 * 3600), dict(RULES, proactive={"enabled": False}),
        st4, lambda *_: None, now=NOW))

    print(f"\n通过 {passed} / {passed + len(failed)}")
    if failed:
        print("失败：", ", ".join(failed))
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(run())
