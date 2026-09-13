"""自动回复 / 转发机器人引擎。

轮询解密后的库，检测被监听会话里的新入站消息，按规则回复或转发。
- 状态存 work/bot_state.json（每会话最后处理的 local_id），只处理启动后的新消息。
- 发送经 docker_wx（容器内 xdotool），目标用会话显示名。

用法： python3 -m core.bot            # 用 bot_rules.json 循环运行
       python3 -m core.bot --once    # 只跑一轮（调试）
"""
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from core import decrypt, messages, contacts, docker_wx, distill, llm  # noqa: E402
from core import imgdec, media, sender, agent, memory, schedule, media_read, send_ledger  # noqa: E402

_DEFAULT_RULES = {
    "poll_interval": 5, "include_self": False, "watch": [],
    "rules": [{"name": "style-reply", "match": {"type": "auto"},
               "action": {"type": "reply_ai", "persona": ""}}],
}
_GLOBAL_RULES = os.path.join(config.PROJECT_DIR, "bot_rules.json")


from core import account_session as sessions, send_ledger, personalization, conversation_state, reply_policy
from core import admin_commands

def rules_file():
    return os.path.join(config.account_dir(), "bot_rules.json")


def state_file():
    return os.path.join(config.account_dir(), "bot_state.json")


# 兼容旧引用
RULES_FILE = property  # 占位，勿直接用


def _scope_of(chat_username):
    """当前会话的记忆作用域(单一实现在 memory.scope_of_chat,工具侧同源)。"""
    return memory.scope_of_chat(chat_username)


def _msg_time_label(create_time, now=None):
    """把一条消息的 unix 时间转成相对标注(刚刚/N分钟前/N小时前/N天前/日期)。
    注意：用本地时区(time.localtime)，不假设服务器 UTC==聊天当地时间。"""
    if not create_time:
        return "时间未知"
    now = now or time.time()
    d = now - create_time
    if d < 90:
        return "刚刚"
    if d < 3600:
        return f"{int(d // 60)}分钟前"
    if d < 86400:
        return f"{int(d // 3600)}小时前"
    if d < 86400 * 7:
        return f"{int(d // 86400)}天前"
    return time.strftime("%m-%d %H:%M", time.localtime(create_time))


def _diag(chat, event, data):
    """脱敏诊断日志：默认关闭(设 WXBOT_DIAG=1 开启)。开启后也只记
    ID/计数/长度/筛选原因等元数据；聊天正文字段(键名带 _text 的)只有再显式设
    WXBOT_DIAG_TEXT=1 才落盘,否则丢弃——两级开关,默认不存任何正文。"""
    if os.environ.get("WXBOT_DIAG") not in ("1", "true", "on"):
        return
    if os.environ.get("WXBOT_DIAG_TEXT") not in ("1", "true", "on"):
        data = {k: v for k, v in data.items() if not k.endswith("_text")}
    try:
        import time as _t
        rec = {"ts": int(_t.time()), "chat": (chat or "")[-8:], "event": event, **data}
        p = os.path.join(config.account_dir(), "diag.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def load_rules():
    f = rules_file()
    if not os.path.exists(f):
        # 首次：迁移旧的全局 bot_rules.json(仅一次)，之后新账号用默认(空 watch)
        src = _GLOBAL_RULES if os.path.exists(_GLOBAL_RULES) else None
        data = json.load(open(src, encoding="utf-8")) if src else dict(_DEFAULT_RULES)
        os.makedirs(config.account_dir(), exist_ok=True)
        json.dump(data, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        if src:                       # 迁移后把全局文件改名，避免新账号继承旧配置
            try:
                os.rename(src, src + ".migrated")
            except OSError:
                pass
    with open(f, encoding="utf-8") as fp:
        return json.load(fp)


def load_state():
    f = state_file()
    st = {}
    if os.path.exists(f):
        try:
            st = json.load(open(f))
        except Exception:  # noqa: BLE001
            st = {}
    # 打上"这份 state 属于哪个账号"的标记，供 save_state 防跨账号串写(见下)。
    st["_acct"] = config.account_key()
    st["_session"] = sessions.capture()
    return st


def save_state(st):
    token = sessions.check()
    if st.get('_acct') != token['account'] or st.get('_session') != token:
        raise sessions.StaleAccount('state_owner_changed')
    sessions.atomic_json(state_file(), st, token)


def pending_file():
    return os.path.join(config.account_dir(), "bot_pending.json")


def save_pending():
    token = sessions.check()
    if _pending_session != token:
        raise sessions.StaleAccount('pending_owner_changed')
    sessions.atomic_json(pending_file(), _pending, token)


def load_pending():
    """Clear old memory, load only this account; prior epochs are held for review."""
    global _pending_session
    _pending.clear()  # including the new-account/no-file path
    _last_learn.clear()
    _last_pat.clear()
    _pending_session = sessions.capture()
    f = pending_file()
    if os.path.exists(f):
        try:
            with open(f, encoding="utf-8") as stream:
                data = json.load(stream)
            if isinstance(data, dict):
                _pending.clear()
                _pending.update(data)
                for p in _pending.values():
                    if p.get('session') != _pending_session:
                        saved = send_ledger.result(p['job_id']) if p.get('job_id') else None
                        if p.get('send_status') == 'uncertain' or (saved and saved['status'] == 'uncertain'):
                            p['send_status'] = 'uncertain'
                        else:
                            p['send_status'] = 'stale'
                        p['reason'] = 'prior_session_requires_review'
                save_pending()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("pending_recovery_failed_" + type(exc).__name__) from exc


def send_name_for(username):
    """会话 username → 发送用显示名（联系人备注/昵称 或 群名）。"""
    if username.endswith("@chatroom"):
        for g in contacts.list_groups():
            if g["username"] == username:
                return g["name"] or username
    else:
        for c in contacts.list_contacts():
            if c["username"] == username:
                return c["name"] or username
    return username


def match_rule(rule, msg, is_group=True):
    m = rule.get("match", {})
    t = m.get("type")
    v = m.get("value", "")
    text = msg.get("content") or ""
    if t == "any":
        return {}
    if t in ("category", "types"):     # 按消息类型匹配(文字/图片/红包/转账/文件/链接…)
        want = v
        if isinstance(want, str):
            want = [w.strip() for w in re.split(r"[,\s，、]+", want) if w.strip()]
        cats = set(want or [])
        cat = msg.get("category")
        if cat not in cats:
            return None
        kw = m.get("value_keyword") or m.get("keyword")   # 可选:类型+关键词双重条件
        if kw and kw not in text:
            return None
        return {"category": cat}
    if t == "keyword":
        return {} if v in text else None
    if t == "regex":
        mo = re.search(v, text)
        if mo:
            return {"group%d" % (i + 1): g for i, g in enumerate(mo.groups())}
    if t == "auto":             # 群里=@我/引用我；私聊=任意消息
        if not is_group:
            return {}
        return {} if (msg.get("at_me") or msg.get("quote_me")) else None
    if t == "mention":          # @我 或 引用我
        return {} if (msg.get("at_me") or msg.get("quote_me")) else None
    if t == "at_me":
        return {} if msg.get("at_me") else None
    if t == "quote_me":
        return {} if msg.get("quote_me") else None
    return None


def render(tpl, text, sender, groups):
    out = tpl.replace("{content}", text).replace("{sender}", sender or "")
    for k, val in (groups or {}).items():
        out = out.replace("{%s}" % k, val or "")
    return out


def _sender_name(wxid):
    for c in contacts.list_contacts():
        if c["username"] == wxid:
            return c["name"]
    return wxid


def _strip_at(text):
    """去掉消息里的 @昵称（含微信的 \\u2005 分隔）。"""
    return re.sub(r"@[^\s ]+[\s ]*", "", text or "").strip()


_media_desc = {}          # (account, chat, message identity) -> successful Result only


def _media_result(chat, m):
    native = media_read.native_voice(m)
    if native:
        return native
    identity = m.get("server_id") or m.get("local_id")
    ck = (config.account_key(), chat, identity)
    if identity is not None and ck in _media_desc:
        return _media_desc[ck]
    result = media_read.read(chat, m, llm.load_cfg())
    _diag(chat, "media_read", {"kind": result.kind, "status": result.status})
    if identity is not None and result.status == "success":
        if len(_media_desc) >= 256:
            _media_desc.pop(next(iter(_media_desc)))
        _media_desc[ck] = result
    return result


def _enrich_media(chat, m):
    if m.get("type") not in media_read.LABELS:
        return m.get("content") or ""
    return _media_result(chat, m).context()


def _passive_content(chat, m):
    # Proactive paths may reuse successful evidence; they never request media analysis.
    if m.get("type") not in media_read.LABELS:
        return m.get("content") or ""
    native = media_read.native_voice(m)
    if native:
        return native.context()
    identity = m.get("server_id") or m.get("local_id")
    result = _media_desc.get((config.account_key(), chat, identity)) if identity is not None else None
    return result.context() if result else media_read.unavailable_context(m)


def _passive_turns(chat, messages_, now):
    from core import reply_context
    turns, _, _ = reply_context.build(
        messages_, [], account=config.wxid(), is_group=chat.endswith('@chatroom'),
        render=lambda cm: _passive_content(chat, cm), name=_sender_name,
        timestamp=lambda cm: _msg_time_label(cm.get('create_time'), now),
        scheduled=lambda cm: schedule.is_scheduled_msg(cm, chat))
    return turns


def _ai_reply(persona, chat_username, msg, context_msgs, rules=None, batch_msgs=None):
    """按真实发言方构造历史轮次，只把当前入站批次作为本轮待回应内容。

    增强：注入对方长期画像；开启 agent 模式时可联网/查历史/查知识库后再答。
    """
    system = (personalization.BEHAVIOR +
              "\n\n【风格 vs 内容·最高优先】本轮角色设定只决定你【说话的语气/风格】,"
              "不决定你是否理会对方。无论对方是否熟人,你都必须真正读懂并回应对方这条消息的"
              "【实际内容和请求】,绝不能把人当陌生人/敌人无视、绝不能用"
              "'没空/晾着/不理你/账号不对劲/我没发过'之类敷衍搪塞、也不能重复同一句话。"
              "\n- 对方提问:先给真实、切题的答案,再谈态度;答案不能被人设情绪(吃醋/防备)顶掉;"
              "\n- 对方要你做的事(查资料/画图等):能做就去做,把结果按你的风格给他;确实做不到就直说做不到。"
              "\n\n【别翻旧账·重要】聊什么由对方【这条消息】决定;下面可能给你一些关于对方的背景资料,"
              "那只是【供你理解】的参考,不是让你主动说出来的话题。别用'我记得你以前…''上次你说…'"
              "去主动翻旧事显示记性;只有对方这次的话真的用得上某条旧信息时,才自然带一句。"
              "熟悉感体现在你能贴切接住他当下的话,不是靠复述过去。他明确问起过去/让你回忆时,才照实回答。"
              "\n\n【多条消息】如果自然语气确实需要停顿或补充，可以输出两段，并用单独一行 [[NEXT]] 分隔；否则只输出一段。不要为了凑数量拆句。"
              "\n\n【表达】像真人聊天,只输出回复正文(不要解释/引号/复述对方原话/重复问候)。"
              "闲聊寒暄就简短口语几句,别凑长;当对方要你说明图片内容、答疑或给结果时,"
              "再把信息说清楚,别为凑短而漏掉该给的内容。")
    system += media_read.HONESTY
    # Memoize failures within this reply too: direct/said/context must agree, no triple requests.
    media_results = {}

    def enrich(cm):
        if cm.get("type") not in media_read.LABELS:
            return cm.get("content") or ""
        key = (cm.get("server_id"), cm.get("local_id"), cm.get("type"))
        if key not in media_results:
            media_results[key] = _media_result(chat_username, cm)
        return media_results[key].context()

    from core import reply_context
    batch = reply_context.unique([cm for cm in (batch_msgs if batch_msgs is not None else [msg])
                                  if not reply_context.is_self(cm, config.wxid())])
    if not batch:
        return ""
    msg = batch[-1]
    if all(cm.get("type") in media_read.LABELS for cm in batch):
        for cm in batch:
            enrich(cm)
        results = [media_results[(cm.get("server_id"), cm.get("local_id"), cm.get("type"))] for cm in batch]
        if all(r.status != "success" for r in results):
            return media_read.failed_reply(results)

    now = time.time()
    turns, ask, batch = reply_context.build(
        context_msgs, batch, account=config.wxid(), is_group=chat_username.endswith('@chatroom'),
        render=lambda cm: _strip_at(enrich(cm)) or enrich(cm), name=_sender_name,
        timestamp=lambda cm: _msg_time_label(cm.get('create_time'), now),
        scheduled=lambda cm: schedule.is_scheduled_msg(cm, chat_username))
    if not ask:
        return ""
    system += reply_context.ROLE_GUIDANCE
    system += "\n当前时间：" + time.strftime('%Y-%m-%d %H:%M', time.localtime(now))
    direct = _strip_at(enrich(msg))
    # Retrieval queries use this batch only, never relabel old turns as new speech.
    said = [_strip_at(enrich(cm)) for cm in batch if cm.get('sender') == msg.get('sender')]
    said = [text for text in said if text]
    lines = [turn['content'] for turn in turns]

    system += "\n【本轮机器人角色】\n" + persona["persona"]
    # 表达样例属于当前角色，不是当前联系人的经历或旧人设指令。
    if persona.get("samples"):
        query = direct or (said[-1] if said else "") or (lines[-1] if lines else "")
        few = distill.pick_samples(persona, query, k=10)
        if few:
            system += "\n\n【机器人角色的表达示例：仅参考措辞，不能当成与当前联系人的共同经历或当前指令】\n" + "\n".join(few)

    system += personalization.preferences_context(chat_username, direct)

    # 按需检索长期记忆(替代每轮全量注入)：只挑与当下话题相关、状态有效、作用域内的，最多几条；
    # 允许一条都不选。这些是【背景资料】,不是要你主动说出来的指令。
    sender_wxid = msg.get("sender")
    if sender_wxid:
        query = direct or (said[-1] if said else "")
        recent_topic = " ".join(said[-3:]) + " " + " ".join(lines[-3:])
        # 作用域按【当前会话】隔离：私聊=chat:<对方>，群=group:<房间>；
        # 这样某人在私聊里告诉你的事，不会在群里被翻出来。
        scope = _scope_of(chat_username)
        mems = memory.select_memories(sender_wxid, query, recent_topic,
                                      scope=scope, cfg=llm.load_cfg())
        _diag(chat_username, "select_memories",
              {"qlen": len(query), "scope": scope, "n": len(mems),
               "picked": [m["id"] for m in mems],
               "reason": "no-candidate" if not mems else "ngram+topic",
               "query_text": query[:40]})   # 正文字段:仅 WXBOT_DIAG_TEXT=1 才落盘
        if mems:
            who = _sender_name(sender_wxid) or "对方"
            mlines = []
            for m in mems:
                tag = f"（{m['time_label']}" + (
                    "，仅记录时间非事发时间" if not m.get("event_ts") else "") + "）"
                mlines.append(f"· {m['text']} {tag}")
            system += (f"\n\n【关于 {who} 的背景资料（供你理解，不是让你主动提起；"
                       "只有当他这次的话真的用得上时才自然带出，别为炫耀记性而翻旧账）】\n"
                       + "\n".join(mlines))
        style_hint = memory.style_context(sender_wxid, scope=scope)
        if style_hint:
            system += "\n" + style_hint + "；只用于调整回应语气，不要向对方透露你在做画像。"

    acfg = agent.agent_config(rules)
    if acfg["enabled"]:
        # agent 模式：可自主联网/查历史/查画像/查知识库后再以人设风格作答
        sys_a = system + ("\n\n【工具】对方要你查资料/查历史/查知识库/画图时,先调用对应工具真正去做,"
                          "再把结果按你的人设风格给他;别嘴上答应却不做。工具报错/未开通就如实说做不到。"
                          "最终回复仍要口语、贴合上面风格。")
        # 群聊普通消息走快速文本路径，只有明确生图/工具请求才启用完整 Agent，
        # 避免工具链长时间占住群监听队列。
        wants_tool = any(k in ask for k in ("画图", "生图", "生成图片", "生成一张图", "查资料", "搜索"))
        tool_names = acfg["tools"] if (not chat_username.endswith("@chatroom") or wants_tool) else []
        out = (agent.run(sys_a, ask, chat=chat_username, history=turns,
                         tool_names=tool_names,
                         display_name=send_name_for(chat_username)) or "").strip()
        return out  # empty tool result must not silently route to the main model
    return llm.chat(system, turns + [{"role": "user", "content": ask}])


_last_pat = {}            # chat -> ts，拍一拍回应节流(防连拍刷屏)
_PAT_COOLDOWN = 60


@sessions.task
def _reply_pat(chat, m, rules, log):
    """有人拍了拍我 → 用人设口吻简短回一句招呼(不走整段AI长回复)。"""
    blocked = sender.preflight(chat)
    if blocked:
        return blocked
    now = time.time()
    if now - _last_pat.get(chat, 0) < _PAT_COOLDOWN:
        log(f"[拍一拍] {chat} 冷却中,跳过")
        return True
    _last_pat[chat] = now
    rule = next((r for r in rules.get("rules", [])
                 if r.get("action", {}).get("type") == "reply_ai"), None)
    persona = personalization.resolve_persona(chat, rules, rule)["persona"]
    text = ""
    try:
        if persona and llm.available():
            sys_p = (personalization.role_context(chat, persona) +
                     "\n\n对方在微信上拍了拍你。用你的口吻回一句简短轻松的招呼"
                     "(一句话,20字以内,像真人随口应一声,别长篇、别翻旧账)。只输出正文。")
            text = (llm.chat(sys_p, [{"role": "user",
                    "content": m.get("content") or "对方拍了拍你"}]) or "").strip()
    except Exception as e:  # noqa: BLE001
        log(f"[拍一拍] 生成失败:{e}")
    if not text:
        text = "嗯?拍我干嘛"
    target = send_name_for(chat)
    r = sender.send_text(target, text, chat_username=chat,
        job_id=send_ledger.stable_id(sessions.capture()["account"], "pat", chat, m.get("local_id")))
    log(f"[拍一拍回应] -> {target}: {text!r} => ok={r.get('ok')}")
    return True


@sessions.task
def greet(chat_username):
    """网页"打招呼"：按该会话最近上下文,用人设生成一句主动问候并发送。返回结构化发送结果。"""
    blocked = sender.preflight(chat_username)
    if blocked:
        return blocked
    gate = conversation_state.ticket(chat_username)
    if not gate or not conversation_state.allowed(gate):
        return {"ok": False, "status": "not_sent", "reason": "proactive_context_changed"}
    rules = load_rules()
    rule = next((r for r in rules.get("rules", [])
                 if r.get("action", {}).get("type") == "reply_ai"), None)
    persona = personalization.resolve_persona(chat_username, rules, rule)["persona"]
    if not persona:
        return {"ok": False, "status": "not_sent", "reason": "generation_unavailable", "message": "未配置 reply_ai 人设"}
    if not llm.available():
        return {"ok": False, "status": "not_sent", "reason": "generation_unavailable", "message": "未配置 LLM"}
    try:
        msgs = messages.get_messages(chat_username, limit=10)
    except Exception:  # noqa: BLE001
        msgs = []
    now = time.time()
    from core import reply_context
    turns = _passive_turns(chat_username, msgs, now)
    ctx = "当前时间：" + time.strftime('%m-%d %H:%M', time.localtime(now))
    system = (personalization.role_context(chat_username, persona) +
              "\n\n【任务】主动给对方发一句打招呼/开场的话：结合上面最近对话的语境自然衔接"
              "(有话题就顺着聊,冷场很久就轻松重新起头),1~2句口语,别翻旧账、别自我介绍、"
              "别重复你刚说过的话。只输出正文。")
    text = (llm.chat(system + media_read.HONESTY + reply_context.ROLE_GUIDANCE, turns + [{"role": "user",
            "content": ctx + "\n【本次任务】请生成一句主动打招呼，承接历史但不要重复自己已说过的话。"}]) or "").strip()
    if not text:
        return {"ok": False, "status": "not_sent", "reason": "generation_unavailable", "message": "生成为空"}
    target = send_name_for(chat_username)
    if not conversation_state.allowed(gate):
        return {"ok": False, "status": "not_sent", "reason": "proactive_context_changed"}
    with reply_policy.scope(chat_username, [], msgs, mode='greeting'):
        r = sender.send_text(target, text, chat_username=chat_username, proactive_ticket=gate)
    if r.get('status') in ('confirmed', 'submitted'):
        return dict(r, message=text)
    message = '发送结果待核对，请勿重复发送' if r.get('status') == 'uncertain' else '本次未发送：' + r.get('reason', '')
    return dict(r, message=message)


@sessions.task
def do_action(rule, msg, chat_username, groups, log, context_msgs=None, rules=None, batch_msgs=None):
    act = rule.get("action", {})
    kind = act.get("type")
    if kind == "reply":
        target = send_name_for(chat_username)
        text = render(act.get("text", ""), msg["content"], msg.get("sender"), groups)
        r = sender.send_text(target, text, chat_username=chat_username)
        log(f"  回复[{rule['name']}] -> {target}: {text!r} => {r}")
    elif kind == "forward":
        target = act.get("to")
        text = render(act.get("prefix", "") + "{content}", msg["content"],
                      msg.get("sender"), groups)
        r = sender.send_text(target, text, chat_username=act.get("to_username"))
        log(f"  转发[{rule['name']}] -> {target}: {text!r} => {r}")
    elif kind == "reply_ai":
        blocked = sender.preflight(chat_username)
        if blocked:
            return blocked
        persona = personalization.resolve_persona(chat_username, rules, rule)["persona"]
        if not persona:
            log(f"  reply_ai 跳过：人设 {act.get('persona')} 不存在")
            return {"ok": False, "status": "not_sent", "reason": "generation_failed", "retryable": False}
        try:
            model_started = time.monotonic()
            text = _ai_reply(persona, chat_username, msg, context_msgs or [], rules, batch_msgs=batch_msgs)
            log(f'[回复耗时] 模型与工具 {round((time.monotonic()-model_started)*1000)}ms')
        except Exception as e:  # noqa: BLE001
            log(f"  reply_ai LLM 出错：{e}")
            return {"ok": False, "status": "not_sent", "reason": "generation_failed", "retryable": False}
        if not (text or "").strip():
            log(f"  AI回复[{persona['name']}] 生成为空，跳过发送(不发空/不空转重试)")
            return {"ok": False, "status": "not_sent", "reason": "generation_failed", "retryable": False}
        target = send_name_for(chat_username)
        parts = [x.strip() for x in re.split(r'\n\s*\[\[NEXT\]\]\s*\n', text) if x.strip()]
        parts = parts[:2]
        r = None
        for index, part in enumerate(parts, 1):
            r = sender.send_text(target, part, chat_username=chat_username,
                                 job_id=send_ledger.stable_id(sessions.capture()['account'], chat_username, 'reply_part', [msg.get('local_id'), index]))
            log(f"  AI回复[{persona['name']}] 第{index}条 -> {target}: {part!r} => {r}")
            if r.get('status') not in ('confirmed', 'submitted'):
                break

    if kind == 'reply_ai' and r.get('status') == 'confirmed':
        try:
            from core import moments_reflection
            moments_reflection.capture(chat_username, batch_msgs or [msg], context_msgs or [], text)
        except Exception:
            log('[朋友圈感悟] 本次触发未完成，聊天回复不受影响')

    return r if kind in ("reply", "forward", "reply_ai") else {"ok": False, "status": "failed", "reason": "unknown_action"}


def _push_summary(msg):
    """把一条消息压成一行可读文本(推送/webhook 用)。文字直接给内容;
    非文字给 [类型名] + 关键 meta(红包/转账金额、文件名、链接标题)。"""
    cat = msg.get("category") or "text"
    name = msg.get("category_name") or ""
    meta = msg.get("meta") or {}
    if cat in ("text",) or msg.get("type") == 1:
        return (msg.get("content") or "").strip() or "[空]"
    if cat in ("red_packet", "transfer"):
        amt = meta.get("amount")
        memo = meta.get("memo") or meta.get("fee_desc") or ""
        s = f"[{name}]"
        if amt:
            s += f" ¥{amt}"
        if memo:
            s += f" {memo}"
        return s
    if cat == "file":
        fn = meta.get("filename") or ""
        return f"[{name}] {fn}".strip()
    if cat in ("link", "miniapp", "video_channel", "merged"):
        title = meta.get("title") or meta.get("des") or ""
        return f"[{name}] {title}".strip()
    # 图片/语音/视频/表情/位置等：content 已是 [图片]/[语音 N"] 之类占位
    return (msg.get("content") or f"[{name}]").strip()


def _username_for_name(name):
    """Resolve a configured name only if unique; an arbitrary first hit is unsafe."""
    if not name:
        return None
    matches = set()
    for lookup in (contacts.list_groups, contacts.list_contacts):
        try:
            for contact in lookup():
                if contact.get('username') == name or contact.get('name') == name:
                    matches.add(contact['username'])
        except Exception:
            return None
    return next(iter(matches)) if len(matches) == 1 else None


def _push_targets(push_cfg):
    """规范化推送目标列表(过滤未启用/空的)。wechat 目标带上解析出的 username(可为 None)。"""
    out = []
    for t in push_cfg.get("targets") or []:
        if not isinstance(t, dict) or t.get("enabled") is False:
            continue
        typ = t.get("type")
        if typ == "wechat" and (t.get("to") or "").strip():
            to = t["to"].strip()
            out.append({"type": "wechat", "to": to,
                        "username": t.get("username") or _username_for_name(to)})
        elif typ == "webhook" and (t.get("url") or "").strip():
            out.append({"type": "webhook", "url": t["url"].strip()})
    return out


import queue as _queue           # noqa: E402
import threading as _threading    # noqa: E402

_push_q = _queue.Queue(maxsize=500)
_push_worker_started = False
_push_worker_lock = _threading.Lock()


def _push_worker():
    """后台推送工人:串行消费推送队列,真正执行发送(微信/webhook)。
    与机器人主循环解耦——推送再慢也不拖住自动回复/定时任务的调度。"""
    while True:
        job = _push_q.get()
        line, payload, targets, log, token = job
        try:
            for index, tg in enumerate(targets):
                try:
                    if tg["type"] == "wechat":
                        # 轻量中继发送:切对会话即发,不做 10s 落库校验(推送量大,要快)
                        if tg.get("username"):
                            r = sender.send_relay(tg["to"], line,
                                                  chat_username=tg["username"], session=token,
                                                  job_id=send_ledger.stable_id(token["account"], "push", payload["chat"], payload["local_id"], index))
                        else:
                            r = sender.send_text(tg["to"], line, session=token,
                                job_id=send_ledger.stable_id(token["account"], "push", payload["chat"], payload["local_id"], index))
                        log(f"  推送->微信[{tg['to']}]: {line!r} => {r}")
                    elif tg["type"] == "webhook":
                        r = sender.send_webhook(tg['url'], payload, session=token,
                            job_id=send_ledger.stable_id(token['account'], 'push', payload['chat'], payload['local_id'], index))
                        log(f"  推送->webhook: status={r.get('status')} reason={r.get('reason')}")
                except Exception as e:  # noqa: BLE001
                    log(f"  推送失败[{tg.get('type')}]: {e}")
        finally:
            _push_q.task_done()


def _ensure_push_worker():
    global _push_worker_started
    with _push_worker_lock:
        if not _push_worker_started:
            t = _threading.Thread(target=_push_worker, daemon=True, name="push-worker")
            t.start()
            _push_worker_started = True


@sessions.task
def _do_push(msg, chat_username, push_cfg, log):
    """把 watch/监听会话里的一条消息【入队】推送到配置的目标(微信好友 / webhook)。
    只做轻量准备+入队后立刻返回,真正发送在后台工人线程,不阻塞机器人主循环。
    不管消息类型,只要来自监听会话就推(可选跳过自己发的)。"""
    targets = _push_targets(push_cfg)
    if not targets:
        return
    chat_name = send_name_for(chat_username)
    sender_wxid = msg.get("sender") or ""
    sender_name = _sender_name(sender_wxid) if sender_wxid else ""
    summary = _push_summary(msg)
    is_group = chat_username.endswith("@chatroom")
    # 时间：微信好友的文本里带上(用户要求),webhook 只放字段、内容不带
    ts = msg.get("create_time") or msg.get("time") or 0
    try:
        tstr = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""
    except Exception:  # noqa: BLE001
        tstr = ""
    # 微信好友收到的文本：时间 [会话] 发送者: 内容
    who = sender_name or sender_wxid or ("我" if msg.get("is_self") else "")
    if is_group:
        body_line = f"[{chat_name}] {who}: {summary}"
    else:
        body_line = f"[{chat_name}] {summary}"
    line = f"{tstr} {body_line}".strip() if tstr else body_line
    payload = {
        "chat": chat_username, "chat_name": chat_name, "is_group": is_group,
        "sender": sender_wxid, "sender_name": sender_name,
        "is_self": bool(msg.get("is_self")),
        "type": msg.get("type"), "category": msg.get("category"),
        "category_name": msg.get("category_name"),
        "content": msg.get("content"), "summary": summary,
        "meta": msg.get("meta") or {},
        "time": tstr, "timestamp": ts,      # webhook 用字段传时间;summary/content 不含时间
        "local_id": msg.get("local_id"),
        "server_id": msg.get("server_id"),
    }
    # 防回环:绝不把某会话的消息推回它自己(否则推送又成新消息→无限乱发)——入队前先滤掉
    send_targets = []
    for tg in targets:
        if tg["type"] == "wechat" and (
                tg["to"] == chat_name
                or (tg.get("username") and tg["username"] == chat_username)):
            continue
        send_targets.append(tg)
    if not send_targets:
        return
    token = sessions.capture()
    ledger = send_ledger.Ledger()
    for index, tg in enumerate(send_targets):
        jid = send_ledger.stable_id(token['account'], 'push', payload['chat'], payload['local_id'], index)
        if tg['type'] == 'wechat':
            ledger.prepare(jid, token, tg.get('username'), 'text', send_ledger.stable_id(line), line)
        elif tg['type'] == 'webhook':
            ledger.prepare(jid, token, 'webhook:' + send_ledger.stable_id(tg['url']),
                           'webhook', send_ledger.stable_id(tg['url'], payload))
    _ensure_push_worker()
    try:
        _push_q.put_nowait((line, payload, send_targets, log, token))
    except _queue.Full:
        log("  推送队列已满，执行意图已保留，待人工核对")


def _expand_watch(watch):
    """watch 含 '*' 时展开为所有群。"""
    if "*" not in watch:
        return watch
    out = [w for w in watch if w != "*"]
    try:
        out += [g["username"] for g in contacts.list_groups()]
    except Exception:  # noqa: BLE001
        pass
    return list(dict.fromkeys(out))


_pending_session = None
_pending = {}          # chat -> {"msgs":[触发消息], "ctx":[...], "rule":.., "last_seen":ts}
_processing = set()     # batches handed to worker threads; incoming messages form next batch
_proactive_busy = set()
_proactive_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='wxbot-proactive')

def _proactive_worker(chat, msgs, rules, state, log, now, token):
    try:
        with sessions.bind(token):
            fn = _maybe_group_nudge if chat.endswith('@chatroom') else _maybe_nudge
            fn(chat, msgs, rules, state, log, now=now)
    except Exception as exc:
        log('[主动回复] ' + type(exc).__name__)
    finally:
        _proactive_busy.discard(chat)

_process_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="wxbot-reply")
SETTLE = 5             # 从首条消息计时，固定五秒批次
MEDIA_SETTLE = 30      # 纯媒体批(只发了图/语音/视频没说话)等更久,给对方补文字的机会


def _settle_for(batch, rules=None):
    """批内有文字→普通去抖;纯媒体→等更久(超时没等到文字就带着图意回)。"""
    if any((pm.get("type") in (1, 49)) for pm in (batch or [])):
        return SETTLE
    return (rules or {}).get("media_settle", MEDIA_SETTLE)


# ---------------- 主动跟进(对方不接话时轻声唤1-2次) ----------------
NUDGE_AFTER = 2 * 3600    # 对方停止交流多久后第一次跟进
NUDGE_GAP = 4 * 3600      # 第一次没回,再隔多久第二次(之后不再发,等对方开口重置)
NUDGE_MAX = 2             # 一轮静默最多跟进次数(不打扰)
NUDGE_QUIET = (22, 8)     # 中国时间夜间静默时段[起,止)不主动跟进


def _quiet_now(now=None, quiet=None):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    h = datetime.fromtimestamp(now if now is not None else time.time(), ZoneInfo('Asia/Shanghai')).hour
    a, b = quiet or NUDGE_QUIET
    return (h >= a or h < b) if a > b else a <= h < b


@sessions.task
def _maybe_nudge(chat, msgs, rules, state, log, now=None):
    """对方停止交流后主动跟进：最多 NUDGE_MAX 次、间隔递增、夜间不发、
    对方一说话立即重置。只在私聊+配了 reply_ai 人设时生效。"""
    now = now or time.time()
    pcfg = rules.get("proactive") or {}
    if pcfg.get("enabled") is not True or pcfg.get('private_share_enabled', True) is not True or chat in _pending or chat in _processing:
        return False
    if sender.preflight(chat):
        return False
    gate = conversation_state.ticket(chat, msgs)
    if gate is None or not conversation_state.allowed(gate):
        return False
    rule = next((r for r in rules.get("rules", [])
                 if r.get("action", {}).get("type") == "reply_ai"), None)
    if not rule or not llm.available():
        return False
    # 只看真实对话(排除定时提醒),取最后一条判断"谁说的最后一句"
    real = [m for m in msgs
            if (m.get("content") or "").strip()
            and not (m.get("is_self") and schedule.is_scheduled_msg(m, chat))]
    if not real:
        return False
    last = real[-1]
    st = state.setdefault("proactive", {}).setdefault(
        chat, {"count": 0, "last_nudge": 0, "last_in": 0})
    last_in = max((m.get("create_time") or 0 for m in real if not m.get("is_self")),
                  default=0)
    if last_in > st.get("last_in", 0):
        st["last_in"] = last_in
        st["count"] = 0                    # 对方有新消息→重置本轮跟进计数
    if not last.get("is_self"):
        return False                       # 最后一句是对方的(在等回复流程),不跟进
    if st["count"] >= int(pcfg.get("max", NUDGE_MAX)):
        return False
    quiet = (pcfg.get('quiet_start', NUDGE_QUIET[0]), pcfg.get('quiet_end', NUDGE_QUIET[1]))
    if _quiet_now(now, quiet):
        return False
    silence = now - (last.get("create_time") or 0)
    need = float(pcfg.get("after", NUDGE_AFTER)) if st["count"] == 0 \
        else float(pcfg.get("gap", NUDGE_GAP))
    if silence < need:
        return False
    persona = personalization.resolve_persona(chat, rules, rule)["persona"]
    if not persona:
        return False
    nudge_id = send_ledger.stable_id(sessions.capture()['account'], 'nudge', chat,
                                   last_in, st['count'], gate['revision'])
    prior = send_ledger.result(nudge_id)
    if prior:
        if prior.get('retryable') and time.time() >= prior.get('retry_at', 0):
            if not conversation_state.allowed(gate):
                return False
            r = sender.retry(nudge_id, sessions.capture())
            st['send_result'] = r
            if r.get('status') in ('confirmed', 'submitted'):
                st['count'] += 1
                st['last_nudge'] = now
                return True
        return False
    from core import reply_context
    turns = _passive_turns(chat, real, now)
    system = (personalization.role_context(chat, persona) +
              "\n\n【任务】对方有一阵子没接话了。你主动轻声说一两句：可以换个轻松的新话题、"
              "围绕已有真实话题、或温和地关心一下;别追问'怎么不回我'、别催、别连环提问、"
              "别翻旧账、别重复你上面已说过的话。1~2句口语即可。只输出正文。")
    ctx = "当前时间：" + time.strftime('%m-%d %H:%M', time.localtime(now))
    text = (llm.chat(system + media_read.HONESTY + reply_context.ROLE_GUIDANCE, turns + [{"role": "user",
            "content": ctx + "\n【本次任务】生成一句主动跟进，不要重复你已说过的话或追问刚问过的问题。"}]) or "").strip()
    if not text:
        return False
    target = send_name_for(chat)
    if not conversation_state.allowed(gate):
        return False
    with reply_policy.scope(chat, [], msgs, mode='nudge', log=log):
        r = sender.send_text(target, text, chat_username=chat, job_id=nudge_id, proactive_ticket=gate)
    st['send_result'] = r
    if r.get('status') not in ('confirmed', 'submitted'):
        return False
    st["count"] += 1
    st["last_nudge"] = now
    log(f"[主动跟进{st['count']}/{int(pcfg.get('max', NUDGE_MAX))}] -> {target}: "
        f"{text!r} => ok={r.get('ok')}")
    return True


@sessions.task
def _maybe_group_nudge(chat, msgs, rules, state, log, now=None):
    """Optional quiet-group reply using the recent conversation window."""
    cfg = rules.get('proactive') or {}
    if cfg.get('enabled') is not True or cfg.get('group_enabled') is not True or chat in _pending or chat in _processing or not msgs:
        return False
    now = now or time.time()
    quiet = (cfg.get('quiet_start', NUDGE_QUIET[0]), cfg.get('quiet_end', NUDGE_QUIET[1]))
    if _quiet_now(now, quiet):
        return False
    after = max(60, float(cfg.get('group_after', 300)))
    real = [m for m in msgs[-20:] if (m.get('content') or '').strip()
            and not (m.get('is_self') and schedule.is_scheduled_msg(m, chat))]
    if not real or real[-1].get('is_self') or now - (real[-1].get('create_time') or 0) < after:
        return False
    rule = next((r for r in rules.get('rules', []) if r.get('action', {}).get('type') == 'reply_ai'), None)
    if not rule or not llm.available() or sender.preflight(chat):
        return False
    st = state.setdefault('group_proactive', {}).setdefault(chat, {'last': 0})
    if now - st.get('last_sent', 0) < max(after, 1800):
        return False
    marker = real[-1].get('local_id') or real[-1].get('server_id')
    if marker == st.get('last'):
        return False
    gate = conversation_state.ticket(chat, real)
    if gate is None or not conversation_state.allowed(gate):
        return False
    persona = personalization.resolve_persona(chat, rules, rule)['persona']
    from core import reply_context
    turns = _passive_turns(chat, real, now)
    prompt = (personalization.role_context(chat, persona) +
              '\n\n【任务】群里已经安静了一段时间。结合最近约十条真实消息，主动接住一个自然话题，发一两句简短内容；不要假装被@，不要催大家，也不要编造群外事实。')
    text = (llm.chat(prompt + media_read.HONESTY + reply_context.ROLE_GUIDANCE, turns + [{'role':'user','content':'请生成一句自然的群聊回复。'}]) or '').strip()
    if not text or not conversation_state.allowed(gate) or chat in _pending or chat in _processing:
        return False
    r = sender.send_text(send_name_for(chat), text, chat_username=chat,
                         job_id=send_ledger.stable_id(sessions.capture()['account'], 'group_nudge', chat, marker),
                         proactive_ticket=gate)
    if r.get('status') in ('confirmed', 'submitted'):
        st['last'] = marker
        st['last_sent'] = now
        log(f'[群主动回复] {chat}: {text!r}')
        return True
    return False

FOLLOW_THRESHOLD = 2   # 群跟发默认阈值：末尾同一句话由 >=2 个【不同的人】发过才算接龙


def _follow_streak(msgs, threshold=FOLLOW_THRESHOLD):
    """检测群消息末尾"多人接龙同一句话"(接龙/+1)。

    关键：要【不同的人】各发一次同样的话才算接龙——同一个人连发两遍不算。
    msgs: 消息列表，按 local_id 升序处理。只统计 type==1 且非撤回的普通文本，
    其余消息(图片/系统/撤回等)从时间线上忽略后再看相邻是否相同。
    从末尾往回取"内容完全相同"的一串，统计其中【去重后的非本人发送者】数量，
    达到阈值(默认2个不同的人)才算。返回 (content, start_local_id, distinct_senders)；
    不足阈值/无文本时返回 None。start_local_id=该串最早一条的 local_id(稳定标识)。
    """
    rel = [m for m in sorted(msgs, key=lambda x: x.get("local_id") or 0)
           if m.get("type") == 1 and not m.get("revoke")]
    if not rel:
        return None
    content = (rel[-1].get("content") or "").strip()
    if not content:
        return None
    senders = set()          # 该串里"不同的人"(排除机器人自己发的那条)
    start_id = rel[-1].get("local_id")
    for m in reversed(rel):
        if (m.get("content") or "").strip() != content:
            break
        start_id = m.get("local_id")
        if not m.get("is_self") and m.get("sender"):
            senders.add(m.get("sender"))
    if len(senders) < int(threshold or FOLLOW_THRESHOLD):
        return None
    return content, start_id, len(senders)


def _follow_key(content, start_id):
    """同一"接龙串"的稳定标识：串最早一条的 local_id + 内容 hash。
    串没断→标识不变(只发一次)；内容变了/新串→最早 local_id 变→新标识(可再触发)。"""
    h = hashlib.md5((content or "").encode("utf-8")).hexdigest()[:12]
    return f"{start_id}:{h}"


@sessions.task
def run_follow(rules, state, log=print):
    """群消息跟发：在勾选的群里，末尾连续相同普通文本 >=阈值 时自动跟发一次。

    去重靠 state['follow_done'][chat] 记录已发的串标识；同一串只发一次，
    机器人自己发出去那条会进串使计数+1，但标识不变故不再发。首次见某群只登记
    当前串、不回放历史。只对群(@chatroom)生效，其它会话绝不打扰。
    """
    follow_watch = [c for c in _expand_watch(rules.get("follow_watch", []))
                    if c.endswith("@chatroom")]
    if not follow_watch:
        return
    threshold = int(rules.get("follow_threshold", FOLLOW_THRESHOLD) or FOLLOW_THRESHOLD)
    done = state.setdefault("follow_done", {})
    for chat in follow_watch:
        try:
            msgs = messages.get_messages(chat, limit=40)
        except Exception as e:  # noqa: BLE001
            log(f"[跟发] 取消息失败 {chat}: {e}")
            continue
        if not msgs:
            continue
        r = _follow_streak(msgs, threshold)
        key = _follow_key(r[0], r[1]) if r else ""
        if chat not in done:            # 首次见此群：只登记当前串，不回放历史
            done[chat] = key
            continue
        if not r:
            continue
        if done.get(chat) == key:       # 这串已跟发过(或历史已登记)——只发一次
            continue
        content, _start, people = r
        target = send_name_for(chat)
        try:
            res = sender.send_text(target, content, chat_username=chat,
                job_id=send_ledger.stable_id(sessions.capture()["account"], "follow", chat, key))
        except Exception as e:  # noqa: BLE001
            log(f"[跟发] 发送失败 {chat}: {e}")
            continue
        if res.get("status") in ('confirmed', 'submitted'):
            done[chat] = key
        log(f"[跟发] {chat} {people}人接龙 -> {target}: {content!r} => {res}")


_last_learn = {}          # chat -> ts，画像抽取节流(避免每轮都调 LLM)
LEARN_INTERVAL = 600      # 每会话最多 10 分钟学一次


@sessions.task
def _maybe_learn(chat, ctx_msgs, log):
    """节流地从最近对话抽取人物画像(长期记忆)。开关：bot_rules/llm_config 的 learn_profiles。"""
    import time as _t
    try:
        if not agent.agent_config().get("enabled") and not llm.available():
            return
        now = _t.time()
        if now - _last_learn.get(chat, 0) < LEARN_INTERVAL:
            return
        _last_learn[chat] = now
        # 先做本地、无模型的风格/近期情绪增量统计；即使 LLM 不可用也不丢失习惯信息。
        memory.update_style_from_messages(ctx_msgs, scope=_scope_of(chat))
        n = memory.extract_from_messages(ctx_msgs, me=config.wxid(),
                                         chat_scope=_scope_of(chat))
        if n:
            log(f"[记忆] {chat} 更新 {n} 人画像")
        # Periodically condense large profiles while retaining source facts outside
        # the active summary. This keeps replies fast without forgetting history.
        for wid in {m.get('sender') for m in ctx_msgs if m.get('sender') and not m.get('is_self')}:
            try:
                prof = memory.load_profile(wid)
                if sum(1 for f in prof.get('facts', []) if f.get('status','active') == 'active') > memory.MAX_FACTS:
                    memory.compress(wid)
            except Exception:
                continue
    except Exception as e:  # noqa: BLE001
        log(f"[记忆] error: {e}")


@sessions.task
def _handle_schedule_msg(chat, m, is_group, log):
    """把一条像"定时/提醒"的聊天消息当作定时任务指令处理，并把回执发回该会话。
    返回是否已处理(处理了就不再走闲聊回复)。"""
    # 用原始内容(不 _strip_at)：@大南是逗比后没空格会被正则连内容一起吃掉；LLM 能正确解析 @
    text = m.get("content") or ""
    ctx = {"chat_username": chat, "chat_display": send_name_for(chat),
           "is_group": is_group, "requester_wxid": m.get("sender"),
           "requester_name": _sender_name(m.get("sender")) or m.get("sender_name")}
    try:
        r = schedule.handle_nl(text, ctx)
    except Exception as e:  # noqa: BLE001
        log(f"[定时] 处理出错: {e}")
        return False
    if r.get("action") == "none":         # LLM 判定不是定时指令→交回普通闲聊
        return False
    if r.get("action") == "list":
        ts = r.get("tasks", [])
        msg = ("当前定时任务：\n" + "\n".join(
            f"· {t['title']} → {t['target_display']}（{t['schedule_desc']}，下次{t['next']}）"
            for t in ts)) if ts else "当前没有定时任务"
    else:
        msg = ("✅ " if r.get("ok") else "⚠️ ") + (r.get("message") or "")
    try:
        sender.send_text(send_name_for(chat), msg, chat_username=chat,
            job_id=send_ledger.stable_id(sessions.capture()["account"], "schedule_receipt", chat, m.get("local_id")))
    except Exception as e:  # noqa: BLE001
        log(f"[定时] 回执发送失败: {e}")
    log(f"[定时] {chat}: {text[:30]!r} => {r.get('message') or r.get('action')}")
    return True


def enqueue_pending(chat, msg, context, rule, now):
    """A completed/held batch must never swallow later incoming messages.

    Save the full old batch durably before replacing its queue slot. The archive
    has no replay worker; uncertain/stale results remain available for review.
    """
    previous = _pending.get(chat)
    carry = []
    if previous and previous.get('send_status') == 'deferred':
        # The dispatch guard proved no UI send occurred. Carry unanswered input
        # into the new batch; completed/uncertain batches are never replayed.
        carry = previous.get('msgs') or []
    if previous and (previous.get('send_status') or previous.get('job_id')):
        send_ledger.Ledger().hold_reply(chat, previous)
        _pending.pop(chat)
    p = _pending.setdefault(chat, dict(msgs=[], ctx=context, rule=rule, session=sessions.capture()))
    from core import reply_context
    p['msgs'] = reply_context.unique(p['msgs'] + carry + [msg])
    p.setdefault('first_seen', now)
    p.update(ctx=context, rule=rule, last_seen=now)
    return p


def _single_closing_reply(p):
    return (p.get('rule', {}).get('action', {}).get('type') in ('reply', 'reply_ai')
            and reply_policy.closing(p.get('msgs') or []))


def process_pending(chat, p, rules, log):
    token = p.get('session')
    if p.get('send_status') == 'uncertain':
        return
    if not sessions.valid(token):
        p.update(send_status='stale', reason='prior_session_requires_review')
        return
    if p.get('send_status') in ('uncertain', 'stale', 'failed'):
        return
    if p.get('send_status') == 'deferred' and p.get('reason') != 'reply_snapshot_unavailable':
        return
    if p.get('send_status') in ('not_sent', 'deferred'):
        child = p.get('child_job_id')
        prior_child = send_ledger.result(child) if child else None
        if not prior_child or not prior_child.get('retryable'):
            return
        if time.time() < prior_child.get('retry_at', 0):
            return
        target_chat = send_ledger.Ledger().get(child)['chat']
        import contextlib
        is_reply = p.get('rule', {}).get('action', {}).get('type') in ('reply', 'reply_ai')
        scope = reply_policy.scope(chat, p['msgs'], p.get('ctx') or [], log=log) if is_reply else contextlib.nullcontext()
        with sessions.bind(token), scope:
            result = sender.retry(child, session=token, display_name=send_name_for(target_chat))
        send_ledger.Ledger().update(p['job_id'], result['status'], result['reason'])
        p.update(send_status=result['status'], reason=result['reason'])
        if result['status'] in ('confirmed', 'submitted', 'skipped'):
            if _pending.get(chat) is p:
                _pending.pop(chat, None)
        send_ledger.Ledger().audit(p['job_id'], token, chat, result['status'], result['reason'], 0)
        save_pending()
        return
    jid = p.setdefault('job_id', send_ledger.stable_id(token['account'], chat,
        'reply', [m.get('local_id') for m in p['msgs']]))
    ledger = send_ledger.Ledger()
    prior = ledger.get(jid)
    decision = reply_policy.decide(p['msgs'], p.get('ctx') or [], token['account'])
    is_reply = p.get('rule', {}).get('action', {}).get('type') in ('reply', 'reply_ai')
    started = time.monotonic()
    if prior:
        res = ledger.view(prior)
    elif is_reply and decision.action == 'observe':
        ledger.prepare(jid, token, chat, 'reply_batch')
        res = ledger.view(ledger.update(jid, 'skipped', decision.reason))
        log('[回复决策] ' + reply_policy.label(decision.reason))
    else:
        ledger.prepare(jid, token, chat, 'reply_batch')
        if not ledger.begin_work(jid):
            return
        save_pending()
        if p.get('last_seen'):
            log(f"[回复耗时] 合并与排队 {round((time.time()-p['last_seen'])*1000)}ms")
        operation = [jid, 0]
        try:
            with sessions.bind(token), send_ledger.operation(jid) as operation:
                import contextlib
                scope = reply_policy.scope(chat, p['msgs'], p.get('ctx') or [], log=log) if is_reply else contextlib.nullcontext()
                with scope:
                    res = do_action(p['rule'], p['msgs'][-1], chat, {}, log,
                        context_msgs=p['ctx'], rules=rules, batch_msgs=p['msgs'])
                res = res or {'status': 'failed', 'reason': 'action_returned_no_result'}
        except sessions.StaleAccount:
            res = {'status': 'stale', 'reason': 'account_session_changed'}
        except Exception as exc:
            res = {'status': 'uncertain', 'reason': 'action_exception_' + type(exc).__name__}
        # A tool may already have sent before model failure or an account switch.
        for index in range(1, operation[1] + 1):
            child = ledger.get(send_ledger.stable_id(jid, index))
            if child and ledger.view(child)['status'] == 'uncertain':
                res = ledger.view(child)
                break
        if res.get('status') == 'deferred':
            children = [ledger.get(send_ledger.stable_id(jid, i)) for i in range(1, operation[1]+1)]
            if any(c and c['status'] in ('initiated', 'submitted', 'confirmed', 'uncertain') for c in children):
                res = dict(status='uncertain', reason='input_changed_after_partial_send')
        p['child_job_id'] = res.get('job_id')
        ledger.update(jid, res.get('status', 'uncertain'), res.get('reason', 'action_result'))
    p['send_status'] = res.get('status', 'uncertain')
    p['reason'] = res.get('reason', '')
    elapsed = round((time.monotonic()-started)*1000)
    ledger.audit(jid, token, chat, p['send_status'], p['reason'], elapsed)
    log(f"[回复处理] {p['send_status']} / {reply_policy.label(p['reason'])} / {elapsed}ms")
    # Never put the old queue in the new account. Ledger already retains its result.
    if not sessions.valid(token):
        return
    if p['send_status'] in ('confirmed', 'submitted', 'skipped'):
        if _pending.get(chat) is p:
            _pending.pop(chat, None)
        save_pending()
        if p['send_status'] != 'skipped':
            _maybe_learn(chat, p.get('ctx') or [], log)
    else:
        save_pending()


@sessions.task
def run_once(rules, state, log=print):
    import time
    sessions.check(state.get('_session'))
    if state.get('_session') != sessions.capture():
        raise sessions.StaleAccount('state_session_changed')
    decrypt.run(force=False)
    include_self = rules.get("include_self", False)
    now = time.time()
    push_cfg = rules.get("push") or {}
    push_on = bool(push_cfg.get("enabled")) and bool(_push_targets(push_cfg))
    push_self = push_cfg.get("include_self", include_self)
    watch_set = set(_expand_watch(rules.get("watch", [])))
    # 管理员私聊即使未加入监听也要能收命令（且不因此触发普通自动回复）。
    admin_set = {a for a in (rules.get("admins") or []) if not a.endswith("@chatroom")}
    process_futures = []
    # push.sources 缺省=监听列表本身;含 '*' 展开为所有群
    push_src = set(_expand_watch(push_cfg.get("sources") or list(watch_set))) if push_on else set()
    # 防回环:推送目标(微信好友/群)本身绝不作为推送来源——否则"推进去的消息"又被当新消息推出去→乱发
    if push_src:
        tgt_names = {t["to"] for t in _push_targets(push_cfg) if t["type"] == "wechat"}
        push_src = {c for c in push_src if send_name_for(c) not in tgt_names}
    for chat in (watch_set | push_src | admin_set):
        msgs = messages.get_messages(chat, limit=40)
        if not msgs:
            continue
        last = state.get(chat)
        maxid = max(m["local_id"] for m in msgs)
        if last is None:                     # 首次：只记录当前位置，不回放历史
            if chat in watch_set and not chat.endswith('@chatroom'):
                conversation_state.observe(chat, msgs)
            state[chat] = maxid
            continue
        # 重建解密库或切换账号后，旧游标可能高于当前库的最大 local_id。
        # 直接校正到当前末尾，避免把之后的新消息全部误判为已处理。
        if last > maxid:
            log(f"[游标校正] {chat}: {last} -> {maxid}")
            state[chat] = maxid
            last = maxid
        # 缺口恢复：两次轮询间若涌入 >窗口 条消息，最老的会滚出 40 条窗口被漏。
        # 只要窗口最小 local_id 仍 > last(没接上上次位置)，就加大窗口重取，直到接上。
        limit = 40
        while last is not None and msgs and min(m["local_id"] for m in msgs) > last \
                and limit < 400:
            limit *= 3
            msgs = messages.get_messages(chat, limit=limit) or msgs
        is_group = chat.endswith("@chatroom")
        if chat in watch_set and not is_group:
            conversation_state.observe(chat, msgs)
        fresh = [m for m in msgs if m["local_id"] > last]
        if chat in watch_set:
            try:
                # Learn every watched conversation, including quiet groups where
                # nobody mentioned the bot; group scope keeps members isolated.
                _maybe_learn(chat, msgs[-60:], log)
                if not is_group:
                    personalization.learn_live(chat, fresh)
            except sessions.StaleAccount:
                raise
            except Exception as exc:
                log("[交流偏好] 增量更新未完成: " + type(exc).__name__)
        for m in sorted(fresh, key=lambda x: x["local_id"]):
            state[chat] = max(state[chat], m["local_id"])
            # 监听推送：来自监听会话的任何消息(不管类型)推给微信好友/webhook
            if push_on and chat in push_src and (push_self or not m["is_self"]):
                _do_push(m, chat, push_cfg, log)
            # 管理员命令：来自已配置管理员的 /命令(私聊任意、群里@我)直接执行系统功能，
            # 执行后跳过本条的普通处理。放在游标推进之后，天然幂等；也在 watch 过滤之前，
            # 让纯管理员会话即使未监听也能收命令。非管理员的 /命令 不拦截、不暴露。
            if not m["is_self"] and m["type"] in (1, 49) and admin_commands.is_admin(m.get("sender"), rules):
                if (m.get("content") or "").lstrip().startswith("/"):
                    engage = (not is_group) or m.get("at_me") or m.get("quote_me")
                    if engage and admin_commands.dispatch(chat, m, is_group, rules, log):
                        continue
            if chat not in watch_set:
                continue                      # 仅推送、不参与自动回复的会话
            if m["is_self"]:
                # 本机器人的定时任务消息：不触发规则/不进待回批(指针已在上面推进,不阻塞)
                if schedule.is_scheduled_msg(m, chat):
                    continue
                if not include_self:
                    continue
            # 拍一拍(拍了拍【我】才应,拍别人不掺和)：简短招呼一句,不走整段AI长回复
            # 实测拍一拍以 type49 appmsg 出现(也兼容 10000 系统消息形态)
            _c_pat = m.get("content") or ""
            if m["type"] in (49, 10000) and "拍了拍我" in _c_pat and len(_c_pat) < 60:
                _reply_pat(chat, m, rules, log)
                continue
            # 文字/appmsg(引用等)走传统文字规则;图片/红包/转账/文件等非文字类只交给"按类型"规则
            text_like = m["type"] in (1, 49)
            # 定时任务指令(仅文字/appmsg)：私聊任意 / 群里@我 时,像"提醒/定时"就直接建任务;
            # 该会话刚触发过提醒时,"不用提醒了/改成六点"这类回应也路由到定时处理(带最近提醒上下文)
            if text_like:
                engage = (not is_group) or m.get("at_me") or m.get("quote_me")
                c_txt = m.get("content") or ""
                sched_like = schedule.looks_schedule(c_txt) or (
                    schedule.looks_reminder_response(c_txt)
                    and schedule.recent_fires(chat=chat, limit=1, within=1800))
                if engage and sched_like:
                    if _handle_schedule_msg(chat, m, is_group, log):
                        break
            # 私聊里图片/语音/视频也可触发AI回复(进去抖队列;纯媒体批用更长等待窗,
            # 见 _settle_for——先等对方补文字,等不到就带着识别出的图意回)。群聊媒体不触发(没法@)。
            media_like = (not is_group) and m["type"] in (3, 34, 43)
            for rule in rules.get("rules", []):
                mt = (rule.get("match") or {}).get("type")
                is_cat_rule = mt in ("category", "types")
                if not is_cat_rule and not (text_like or media_like):
                    continue           # 传统文字规则不作用于其余非文字消息(系统类等)
                g = match_rule(rule, m, is_group)
                if g is None:
                    continue
                if rule.get("action", {}).get("type") == "reply_ai":
                    # 攒进待回队列，去抖后一次性回
                    p = enqueue_pending(chat, m, msgs, rule, now)
                    log(f"[攒:{rule['name']}] {chat} +1条(共{len(p['msgs'])})")
                else:
                    enqueue_pending(chat, m, msgs, rule, now)
                break
        # Slow proactive generation/UI never holds up polling other contacts.
        if chat in watch_set and chat not in _proactive_busy:
            _proactive_busy.add(chat)
            _proactive_pool.submit(_proactive_worker, chat, msgs, rules, state, log, now, sessions.capture())
    save_pending()
    save_state(state)  # cursor only advances durably after pending has committed
    # 去抖：对方停顿够久 → 综合最近这批消息回一次
    for chat in list(_pending.keys()):
        if chat in _processing:
            continue
        p = _pending[chat]
        if not p.get("msgs"):
            _pending.pop(chat, None)
            continue
        if now - p.get("first_seen", p.get("last_seen", 0)) >= _settle_for(p["msgs"], rules):
            trig = p["msgs"][-1]
            if not p.get('send_status'):
                log(f"[待回复批次] {chat} 综合 {len(p['msgs'])} 条")
            # Freeze this batch and hand slow model/UI work to a worker. New
            # inbound messages can immediately create the next batch.
            send_ledger.Ledger().hold_reply(chat, p)
            _pending.pop(chat, None)
            _processing.add(chat)
            process_futures.append(_process_pool.submit(_process_worker, chat, p, rules, log))
    # 群消息跟发(接龙/+1)——与上面的回复逻辑并行独立，互不影响
    try:
        run_follow(rules, state, log)
    except Exception as e:  # noqa: BLE001
        log(f"[跟发] error: {e}")
    save_pending()
    save_state(state)
    return process_futures


def _process_worker(chat, p, rules, log):
    try:
        process_pending(chat, p, rules, log)
    finally:
        if sessions.valid(p.get('session')) and p.get('send_status') not in ('confirmed', 'submitted', 'skipped'):
            if chat not in _pending:
                _pending[chat] = p
            else:
                send_ledger.Ledger().hold_reply(chat, p)
            save_pending()
        _processing.discard(chat)


def main():
    rules = load_rules()
    state = load_state()
    load_pending()
    once = "--once" in sys.argv
    print(f"[bot] 监听 {rules.get('watch')}, include_self={rules.get('include_self')}, "
          f"规则 {len(rules.get('rules', []))} 条")
    if once:
        run_once(rules, state)
        return
    from core import reply_inbox
    reply_inbox.start(lambda: True)
    interval = rules.get("poll_interval", 5)
    while True:
        try:
            if state.get("_session") != sessions.observe():
                state = load_state()
                load_pending()
            rules = load_rules()          # 热加载规则
            run_once(rules, state)
        except Exception as e:            # noqa: BLE001
            print("[bot] error:", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
