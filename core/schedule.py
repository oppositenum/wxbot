"""自然语言定时任务：用描述创建/取消/列出定时任务，到点把处理结果发给对应的人。

- 私聊目标：结果直接发给他。
- 群聊目标：结果通过【@他】发到群里。
- 任务持久化到 accounts/<wxid>/schedules.json；调度线程每 ~20s tick 一次。
- prompt 处理：use_llm=True 时把 prompt 过一遍 LLM(可带人设)产出内容；否则原样发。
- 支持 cron(5 字段) 或 once_at(一次性, YYYY-MM-DD HH:MM)。
"""
import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from core import contacts, llm  # noqa: E402

_LOG = []


def _log(m):
    _LOG.append(f"{time.strftime('%m-%d %H:%M:%S')} {m}")
    _LOG[:] = _LOG[-100:]
    print("[schedule]", m)


def logs():
    return list(_LOG)


def _file():
    return os.path.join(config.account_dir(), "schedules.json")


def load_tasks():
    try:
        return json.load(open(_file(), encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []


def save_tasks(tasks):
    os.makedirs(config.account_dir(), exist_ok=True)
    json.dump(tasks, open(_file(), "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ---------------- 名字解析 ----------------
def _norm(s):
    s = re.sub(r"\s|　| ", "", (s or "")).lower()
    return re.sub(r"(群聊|群)$", "", s)      # 去掉末尾群字,容忍 test-001群


def resolve_target(name):
    """把目标名字解析成 (username, is_group, display_name)。找不到返回 (None,None,None)。
    群名优先按群匹配；否则按联系人(备注/昵称/微信号)匹配。"""
    n = _norm(name)
    if not n:
        return None, None, None
    # 群
    best = None
    for g in contacts.list_groups():
        gn = _norm(g.get("name"))
        if gn and (gn == n or n in gn or gn in n):
            if gn == n:
                return g["username"], True, g.get("name")
            best = best or (g["username"], True, g.get("name"))
    # 联系人
    for c in contacts.list_contacts():
        for field in ("remark", "nick_name", "name", "alias"):
            fv = _norm(c.get(field))
            if fv and fv == n:
                return c["username"], False, (c.get("remark") or c.get("nick_name")
                                              or c.get("name") or name)
    for c in contacts.list_contacts():
        for field in ("remark", "nick_name", "name"):
            fv = _norm(c.get(field))
            if fv and (n in fv or fv in n):
                best = best or (c["username"], False, (c.get("remark") or c.get("nick_name")
                                                       or c.get("name") or name))
    return best or (None, None, None)


def resolve_member(group_username, name):
    """群里按名字找成员，返回 (wxid, 群显示名)。找不到返回 (None, name)。"""
    n = _norm(name)
    if not n:
        return None, name
    exact = None
    for m in contacts.group_members(group_username):
        for field in ("group_nick", "remark", "nick_name", "name"):
            fv = _norm(m.get(field))
            if fv and fv == n:
                return m["wxid"], (m.get("group_nick") or m.get("name") or name)
    for m in contacts.group_members(group_username):
        for field in ("group_nick", "remark", "nick_name", "name"):
            fv = _norm(m.get(field))
            if fv and (n in fv or fv in n):
                exact = exact or (m["wxid"], (m.get("group_nick") or m.get("name") or name))
    return exact or (None, name)


# ---------------- cron ----------------
def _field_match(expr, val):
    for part in str(expr).split(","):
        part = part.strip()
        if part in ("*", ""):
            return True
        m = re.fullmatch(r"\*/(\d+)", part)
        if m:
            if val % int(m.group(1)) == 0:
                return True
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            if int(m.group(1)) <= val <= int(m.group(2)):
                return True
            continue
        if part.isdigit() and int(part) == val:
            return True
    return False


def cron_match(cron, tm):
    """cron: '分 时 日 月 周'(周: 0/7=周日)。tm=time.struct_time。"""
    try:
        mi, ho, dom, mo, dow = cron.split()
    except Exception:  # noqa: BLE001
        return False
    wd = (tm.tm_wday + 1) % 7        # python: Mon=0 → cron: Sun=0
    return (_field_match(mi, tm.tm_min) and _field_match(ho, tm.tm_hour)
            and _field_match(dom, tm.tm_mday) and _field_match(mo, tm.tm_mon)
            and (_field_match(dow, wd) or _field_match(dow, 7 if wd == 0 else wd)))


def describe_schedule(t):
    if t.get("once_at"):
        return "一次性 " + t["once_at"]
    return "cron " + (t.get("cron") or "?")


def _next_hint(t):
    """给个粗略的下次触发提示(最多向后找 8 天)。"""
    if t.get("once_at"):
        return t["once_at"]
    if not t.get("cron"):
        return "?"
    now = time.time()
    for i in range(1, 8 * 24 * 60):
        tm = time.localtime(now + i * 60)
        if cron_match(t["cron"], tm):
            return time.strftime("%m-%d %H:%M", tm)
    return "?"


# ---------------- 触发 ----------------
def _compute_text(t):
    prompt = t.get("prompt") or ""
    if not t.get("use_llm"):
        return prompt
    try:
        from core import distill
        system = "你是定时助手，按要求生成一条要发送的消息，直接输出消息正文，简洁自然。"
        persona = t.get("persona")
        if persona:
            p = distill.load_persona(persona)
            if p:
                system = p["persona"] + "\n\n按下面要求生成一条要发送的消息，只输出正文。"
        return llm.chat(system, [{"role": "user", "content": prompt}]) or prompt
    except Exception as e:  # noqa: BLE001
        _log(f"LLM 处理失败,改发原文: {e}")
        return prompt


def fire(t):
    from core import sender
    text = _compute_text(t)
    if not text:
        _log(f"任务[{t.get('title')}] 无内容,跳过")
        return
    uname = t.get("target_username")
    disp = t.get("target_display") or t.get("target")
    if not uname:
        _log(f"任务[{t.get('title')}] 目标未解析,跳过")
        return
    if t.get("is_group") and t.get("mention_wxid"):
        r = sender.send_at(disp, uname, t.get("mention_wxid"),
                           t.get("mention_display") or "", text)
    else:
        r = sender.send_text(disp, text, chat_username=uname)
    _log(f"任务[{t.get('title')}] -> {disp}: {text[:30]!r} => ok={r.get('ok')}")


def tick():
    tasks = load_tasks()
    changed = False
    now = time.time()
    tm = time.localtime(now)
    cur_min = int(now // 60)
    remaining = []
    for t in tasks:
        keep = True
        try:
            if not t.get("enabled", True):
                remaining.append(t)
                continue
            due = False
            if t.get("once_at"):
                try:
                    at = time.mktime(time.strptime(t["once_at"], "%Y-%m-%d %H:%M"))
                    due = now >= at
                except Exception:  # noqa: BLE001
                    due = False
            elif t.get("cron"):
                due = cron_match(t["cron"], tm) and t.get("last_min") != cur_min
            if due:
                t["last_min"] = cur_min
                t["last_fired"] = time.strftime("%Y-%m-%d %H:%M", tm)
                changed = True
                fire(t)
                if t.get("once_at"):
                    keep = False        # 一次性任务触发后删除
        except Exception as e:  # noqa: BLE001
            _log(f"tick 任务出错: {e}")
        if keep:
            remaining.append(t)
    if changed:
        save_tasks(remaining)


_loop_started = [False]


def start_loop():
    if _loop_started[0]:
        return
    _loop_started[0] = True

    def _run():
        while True:
            try:
                tick()
            except Exception as e:  # noqa: BLE001
                _log(f"loop 出错: {e}")
            time.sleep(20)
    threading.Thread(target=_run, daemon=True).start()
    _log("调度线程已启动")


# ---------------- 增删查 ----------------
def _new_id(tasks):
    return (max([t.get("id", 0) for t in tasks], default=0) + 1)


def add_task(title, target, prompt, cron=None, once_at=None, mention=None,
             use_llm=False, persona=None):
    """创建任务，自动解析目标/被@成员。返回 (task, error)。"""
    uname, is_group, disp = resolve_target(target)
    if not uname:
        return None, f"找不到会话/联系人：{target}"
    mention_wxid = mention_disp = None
    if is_group and mention:
        mention_wxid, mention_disp = resolve_member(uname, mention)
        if not mention_wxid:
            return None, f"群「{disp}」里找不到成员：{mention}"
    if not cron and not once_at:
        return None, "没有解析到时间(cron 或 once_at)"
    tasks = load_tasks()
    t = {"id": _new_id(tasks), "title": title or (prompt or "定时任务")[:16],
         "target": target, "target_username": uname, "target_display": disp,
         "is_group": bool(is_group), "mention": mention, "mention_wxid": mention_wxid,
         "mention_display": mention_disp, "cron": cron, "once_at": once_at,
         "prompt": prompt, "use_llm": bool(use_llm), "persona": persona,
         "enabled": True, "created": time.strftime("%Y-%m-%d %H:%M")}
    tasks.append(t)
    save_tasks(tasks)
    _log(f"新建任务[{t['title']}] -> {disp} @{describe_schedule(t)}")
    return t, None


def remove_task(id=None, match=None):
    """按 id 删除；或按 match(标题/目标模糊匹配) 删除。返回删除数。"""
    tasks = load_tasks()
    keep, removed = [], 0
    for t in tasks:
        hit = False
        if id is not None and t.get("id") == id:
            hit = True
        elif match:
            m = _norm(match)
            hay = _norm(t.get("title")) + " " + _norm(t.get("target")) + " " + _norm(t.get("prompt"))
            if m and (m in hay
                      # 宽松:匹配串的任意 2-gram 命中即算(容忍"周报提醒"↔"提醒交周报"词序不同)
                      or any(m[i:i+2] in hay for i in range(len(m) - 1) if len(m) >= 2)):
                hit = True
        if hit:
            removed += 1
        else:
            keep.append(t)
    if removed:
        save_tasks(keep)
    return removed


def list_view():
    out = []
    for t in load_tasks():
        out.append({**t, "schedule_desc": describe_schedule(t), "next": _next_hint(t)})
    return out


# ---------------- 自然语言 ----------------
_NL_SYSTEM = """你把用户对"定时任务"的自然语言指令解析成 JSON。任务到点会把结果发给某人:私聊直发,群聊@他。
严格只输出 JSON,字段:
{
  "action": "create|cancel|list",     // 新建/取消/列出
  "title": "简短标题(<=16字)",
  "target": "发给谁 或 哪个群——【原样完整照抄名字,含数字/连字符,如 test-001 不要写成 test】",
  "mention": "群里要@的人名(原样完整照抄;私聊或无需@则空串)",
  "cron": "分 时 日 月 周 (标准5字段cron)，非周期性任务给空串",
  "once_at": "YYYY-MM-DD HH:MM (一次性任务的绝对时间)，周期性给空串",
  "prompt": "到点要做/要说的内容",
  "use_llm": true/false,              // 内容需要AI临时生成(如"总结今天新闻")=true; 固定话术(如"提醒交周报")=false
  "cancel_match": "取消/删除时用来匹配已有任务的关键词(action=cancel时给)"
}
规则:
- "每天9点"→cron "0 9 * * *"; "每周一10:30"→"30 10 * * 1"; "每小时"→"0 * * * *"; "每30分钟"→"*/30 * * * *"。
- "明天下午3点"这种一次性→用 once_at 绝对时间(基于下面给的当前时间推算)。
- 只有取消意图→action=cancel,填 cancel_match;只有查看意图→action=list。
- 不确定的字段给空串。不要输出 JSON 以外任何字。"""


def parse_nl(text):
    now = time.strftime("%Y-%m-%d %H:%M %A")
    raw = llm.chat(_NL_SYSTEM, [{"role": "user",
                   "content": f"当前时间:{now}\n指令:{text}"}])
    raw = raw.replace("```json", "").replace("```", "")
    raw = raw[raw.find("{"): raw.rfind("}") + 1]
    return json.loads(raw)


_SCHED_VERB = re.compile(r"提醒|定时|闹钟|催我|叫我|通知我")
_SCHED_TIME = re.compile(
    r"每[天周月日]|每隔|每小时|每分钟|每\d|明天|后天|今天|上午|下午|晚上|早上|凌晨|"
    r"周[一二三四五六日天]|礼拜|\d+\s*点|\d{1,2}:\d{2}|过\d+分|一分钟|半小时")
_SCHED_MANAGE = re.compile(r"(取消|删除|关掉|停掉|列出|查看|有哪些|看看).{0,8}(任务|提醒|定时|闹钟)")


def looks_schedule(text):
    """粗判一句话是不是"定时任务"指令(用于机器人聊天里识别,避免每句都调LLM)。"""
    if not text:
        return False
    if _SCHED_MANAGE.search(text):
        return True
    return bool(_SCHED_VERB.search(text) and _SCHED_TIME.search(text))


_SELF_WORDS = {"我", "自己", "me", "本人", "俺"}


def handle_nl(text, ctx=None):
    """解析并执行一条自然语言定时指令。返回 {ok, action, message, ...}。
    ctx(可选)={chat_username, chat_display, is_group, requester_wxid, requester_name}：
    指令来自聊天时用作默认目标——说"提醒我"就发回给说话的人(私聊直发,群里@他)。"""
    if not llm.available():
        return {"ok": False, "message": "未配置 LLM,无法解析自然语言"}
    try:
        d = parse_nl(text)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"解析失败: {e}"}
    action = (d.get("action") or "create").lower()
    if action == "list":
        return {"ok": True, "action": "list", "tasks": list_view()}
    if action == "cancel":
        n = remove_task(match=d.get("cancel_match") or d.get("target") or d.get("title"))
        return {"ok": n > 0, "action": "cancel",
                "message": f"已取消 {n} 个任务" if n else "没找到匹配的任务"}
    # create——目标为空或"我/自己"时,用指令来源(说话的人/所在会话)作默认目标
    tgt = (d.get("target") or "").strip()
    mention = (d.get("mention") or "").strip() or None
    if ctx and (not tgt or _norm(tgt) in {_norm(w) for w in _SELF_WORDS}):
        if ctx.get("is_group"):
            tgt = ctx.get("chat_display") or ctx.get("chat_username")
            mention = mention or ctx.get("requester_name")   # 群里默认@提出人
        else:
            tgt = ctx.get("chat_display") or ctx.get("chat_username")
    t, err = add_task(
        title=d.get("title"), target=tgt, prompt=d.get("prompt"),
        cron=(d.get("cron") or "").strip() or None,
        once_at=(d.get("once_at") or "").strip() or None,
        mention=mention, use_llm=bool(d.get("use_llm")))
    if err:
        return {"ok": False, "action": "create", "message": err, "parsed": d}
    return {"ok": True, "action": "create",
            "message": f"已创建:「{t['title']}」→ {t['target_display']} "
                       f"({describe_schedule(t)}, 下次 {_next_hint(t)})", "task": t}
