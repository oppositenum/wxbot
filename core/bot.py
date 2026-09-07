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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from core import decrypt, messages, contacts, docker_wx, distill, llm  # noqa: E402
from core import imgdec, media, sender, agent, memory, schedule  # noqa: E402

_DEFAULT_RULES = {
    "poll_interval": 5, "include_self": False, "watch": [],
    "rules": [{"name": "style-reply", "match": {"type": "auto"},
               "action": {"type": "reply_ai", "persona": ""}}],
}
_GLOBAL_RULES = os.path.join(config.PROJECT_DIR, "bot_rules.json")


def rules_file():
    return os.path.join(config.account_dir(), "bot_rules.json")


def state_file():
    return os.path.join(config.account_dir(), "bot_state.json")


# 兼容旧引用
RULES_FILE = property  # 占位，勿直接用


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
    if os.path.exists(f):
        try:
            return json.load(open(f))
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_state(st):
    config.ensure_dirs()
    json.dump(st, open(state_file(), "w"), indent=2)


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


_media_desc = {}          # (chat, server_id) -> 描述文本(缓存，避免重复调用视觉/STT)


def _enrich_media(chat, m):
    """媒体消息 → 一句可读描述，让大模型"看懂/听懂"。
    图片/视频封面走视觉模型，语音走 STT；解不出就退回占位符。成功才缓存。"""
    t = m.get("type")
    if t not in (3, 34, 43):
        return m.get("content")
    sid = m.get("server_id")
    ck = (chat, sid)
    if ck in _media_desc:
        return _media_desc[ck]
    lid = m.get("local_id")
    out = m.get("content")                # 兜底占位：[图片]/[语音 N"]/[视频 N"]
    try:
        if t == 3:
            data, _ = imgdec.get_msg_image(chat, lid)
            if data:
                d = llm.describe_image(data)
                if d:
                    out = f"[图片：{d}]"
        elif t == 43:
            data, _ = media.get_msg_video_thumb(chat, lid)
            if data:
                d = llm.describe_image(data)
                if d:
                    out = f"[视频（封面）：{d}]"
        elif t == 34:
            data, mime = media.get_msg_voice(chat, lid)
            if data:
                tx = llm.transcribe(data, content_type=mime or "audio/mpeg")
                if tx:
                    out = f"[语音：{tx}]"
    except Exception:  # noqa: BLE001
        pass
    if sid and out != m.get("content"):   # 只缓存成功的描述，失败下次可重试
        _media_desc[ck] = out
    return out


def _ai_reply(persona, chat_username, msg, context_msgs, rules=None):
    """用 LLM 以蒸馏人设生成回复。处理"先说话再单独@"：@那条没内容时，
    用该发送者最近连发的几句作为要回应的内容。

    增强：注入对方长期画像；开启 agent 模式时可联网/查历史/查知识库后再答。
    """
    system = (persona["persona"] +
              "\n\n【模仿要求】结合下面的对话上下文来回应,像真人聊天。"
              "只输出回复内容,1~2句、口语化、简短自然,贴合上面风格;"
              "不要解释、不要加引号、不要逐句复述对方的话、不要重复问候。")
    # 注入对方长期画像(记得住人)
    prof = memory.profile_context(msg.get("sender")) if msg.get("sender") else ""
    if prof:
        system += "\n\n" + prof

    asker = _sender_name(msg.get("sender")) or "群友"
    # @那条里@之外的实质内容；若是图片/语音/视频，替换成模型看懂/听懂后的描述
    direct = _strip_at(_enrich_media(chat_username, msg))

    # 该发送者最近连发的（含图片/语音/视频的可读描述）
    said = []
    for cm in context_msgs[-16:]:
        if cm.get("sender") == msg.get("sender") and not cm.get("is_self"):
            t = _strip_at(_enrich_media(chat_username, cm))
            if t:
                said.append(t)

    # 全员最近对话上下文（媒体也转成可读描述）
    lines = []
    for cm in context_msgs[-8:]:
        who = "我" if cm.get("is_self") else (_sender_name(cm.get("sender")) or "对方")
        c = _enrich_media(chat_username, cm)
        if c:
            lines.append(f"{who}: {_strip_at(c) or c}")
    ctx = "群里最近对话：\n" + "\n".join(lines)

    if said and len(said) > 1:
        ask = (f"\n\n{asker} 刚连着说了这几句：{' / '.join(said[-6:])}"
               f"{'（并@了你）' if direct == '' and msg.get('at_me') else ''}。"
               f"请综合起来、以你的风格回 1~2 句：")
    elif direct:
        ask = f"\n\n{asker} 说：{direct}\n请以你的风格回 1~2 句："
    elif said:
        ask = f"\n\n{asker} 说：{said[-1]}\n请以你的风格回 1~2 句："
    else:
        ask = f"\n\n{asker} @了你。请结合上文、以你的风格接 1~2 句："

    # 检索式 few-shot：按当前话题从样例库挑最像的历史原话，比固定取最近若干条更贴本人
    if persona.get("samples"):
        query = direct or (said[-1] if said else "") or (lines[-1] if lines else "")
        few = distill.pick_samples(persona, query, k=10)
        if few:
            system += "\n\n【与当前话题最接近的本人历史原话(模仿口吻，别照抄)】\n" + "\n".join(few)

    acfg = agent.agent_config(rules)
    if acfg["enabled"]:
        # agent 模式：可自主联网/查历史/查画像/查知识库后再以人设风格作答
        sys_a = system + ("\n\n【工具】必要时可查资料/历史/知识库后再答，但最终回复仍要"
                          "简短口语、贴合上面风格。")
        return agent.run(sys_a, ctx + ask, chat=chat_username,
                         tool_names=acfg["tools"])
    return llm.chat(system, [{"role": "user", "content": ctx + ask}])


def do_action(rule, msg, chat_username, groups, log, context_msgs=None, rules=None):
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
        r = docker_wx.send_text(target, text)
        log(f"  转发[{rule['name']}] -> {target}: {text!r} => {r}")
    elif kind == "reply_ai":
        persona = distill.load_persona(act.get("persona", ""))
        if not persona:
            log(f"  reply_ai 跳过：人设 {act.get('persona')} 不存在")
            return
        try:
            text = _ai_reply(persona, chat_username, msg, context_msgs or [], rules)
        except Exception as e:  # noqa: BLE001
            log(f"  reply_ai LLM 出错：{e}")
            return
        target = send_name_for(chat_username)
        r = sender.send_text(target, text, chat_username=chat_username)
        log(f"  AI回复[{persona['name']}] -> {target}: {text!r} => {r}")


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


_pending = {}          # chat -> {"msgs":[触发消息], "ctx":[...], "rule":.., "last_seen":ts}
SETTLE = 8             # 对方停顿这么多秒才回（去抖，避免一句一回）

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
            res = sender.send_text(target, content, chat_username=chat)
        except Exception as e:  # noqa: BLE001
            log(f"[跟发] 发送失败 {chat}: {e}")
            continue
        done[chat] = key                # 发完立即标记，避免下轮重复
        docker_wx.note_open(chat)       # 发送后微信停在此会话
        log(f"[跟发] {chat} {people}人接龙 -> {target}: {content!r} => {res}")


_last_learn = {}          # chat -> ts，画像抽取节流(避免每轮都调 LLM)
LEARN_INTERVAL = 600      # 每会话最多 10 分钟学一次


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
        n = memory.extract_from_messages(ctx_msgs, me=config.wxid())
        if n:
            log(f"[记忆] {chat} 更新 {n} 人画像")
    except Exception as e:  # noqa: BLE001
        log(f"[记忆] error: {e}")


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
    if r.get("action") == "list":
        ts = r.get("tasks", [])
        msg = ("当前定时任务：\n" + "\n".join(
            f"· {t['title']} → {t['target_display']}（{t['schedule_desc']}，下次{t['next']}）"
            for t in ts)) if ts else "当前没有定时任务"
    else:
        msg = ("✅ " if r.get("ok") else "⚠️ ") + (r.get("message") or "")
    try:
        sender.send_text(send_name_for(chat), msg, chat_username=chat)
    except Exception as e:  # noqa: BLE001
        log(f"[定时] 回执发送失败: {e}")
    log(f"[定时] {chat}: {text[:30]!r} => {r.get('message') or r.get('action')}")
    return True


def run_once(rules, state, log=print):
    import time
    decrypt.run(force=False)
    include_self = rules.get("include_self", False)
    now = time.time()
    for chat in _expand_watch(rules.get("watch", [])):
        msgs = messages.get_messages(chat, limit=40)
        if not msgs:
            continue
        last = state.get(chat)
        maxid = max(m["local_id"] for m in msgs)
        if last is None:                     # 首次：只记录当前位置，不回放历史
            state[chat] = maxid
            continue
        # 缺口恢复：两次轮询间若涌入 >窗口 条消息，最老的会滚出 40 条窗口被漏。
        # 只要窗口最小 local_id 仍 > last(没接上上次位置)，就加大窗口重取，直到接上。
        limit = 40
        while last is not None and msgs and min(m["local_id"] for m in msgs) > last \
                and limit < 400:
            limit *= 3
            msgs = messages.get_messages(chat, limit=limit) or msgs
        is_group = chat.endswith("@chatroom")
        fresh = [m for m in msgs if m["local_id"] > last]
        for m in sorted(fresh, key=lambda x: x["local_id"]):
            state[chat] = max(state[chat], m["local_id"])
            if m["type"] not in (1, 49):
                continue
            if m["is_self"] and not include_self:
                continue
            # 定时任务指令：私聊任意 / 群里@我 时，若像"提醒/定时"就直接建任务并回执(不走闲聊)
            engage = (not is_group) or m.get("at_me") or m.get("quote_me")
            if engage and schedule.looks_schedule(m.get("content") or ""):
                if _handle_schedule_msg(chat, m, is_group, log):
                    break
            for rule in rules.get("rules", []):
                g = match_rule(rule, m, is_group)
                if g is None:
                    continue
                if rule.get("action", {}).get("type") == "reply_ai":
                    # 攒进待回队列，去抖后一次性回
                    p = _pending.setdefault(chat, {"msgs": [], "ctx": msgs, "rule": rule})
                    p["msgs"].append(m)
                    p["ctx"] = msgs
                    p["rule"] = rule
                    p["last_seen"] = now
                    log(f"[攒:{rule['name']}] {chat} +1条(共{len(p['msgs'])})")
                else:
                    do_action(rule, m, chat, g, log, context_msgs=msgs, rules=rules)
                break
    # 去抖：对方停顿够久 → 综合最近这批消息回一次
    for chat in list(_pending.keys()):
        p = _pending[chat]
        if not p.get("msgs"):
            _pending.pop(chat, None)
            continue
        if now - p.get("last_seen", 0) >= SETTLE:
            trig = p["msgs"][-1]
            log(f"[AI批量回复] {chat} 综合 {len(p['msgs'])} 条")
            do_action(p["rule"], trig, chat, {}, log, context_msgs=p["ctx"], rules=rules)
            _maybe_learn(chat, p.get("ctx") or [], log)
            _pending.pop(chat, None)
    # 群消息跟发(接龙/+1)——与上面的回复逻辑并行独立，互不影响
    try:
        run_follow(rules, state, log)
    except Exception as e:  # noqa: BLE001
        log(f"[跟发] error: {e}")
    save_state(state)


def main():
    rules = load_rules()
    state = load_state()
    once = "--once" in sys.argv
    print(f"[bot] 监听 {rules.get('watch')}, include_self={rules.get('include_self')}, "
          f"规则 {len(rules.get('rules', []))} 条")
    if once:
        run_once(rules, state)
        return
    interval = rules.get("poll_interval", 5)
    while True:
        try:
            rules = load_rules()          # 热加载规则
            run_once(rules, state)
        except Exception as e:            # noqa: BLE001
            print("[bot] error:", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
