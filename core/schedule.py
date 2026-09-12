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
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

CHINA = ZoneInfo("Asia/Shanghai")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from core import account_session as sessions, send_ledger
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
        with open(_file(), encoding="utf-8") as source:
            return json.load(source)
    except Exception:  # noqa: BLE001
        return []


def save_tasks(tasks):
    os.makedirs(config.account_dir(), exist_ok=True)
    sessions.atomic_json(_file(), tasks)


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
    from core import distill
    moment=datetime.fromtimestamp(t.get('_trigger_at',time.time()),CHINA)
    system='你是定时助手，只生成本次触发时刻对应的一条提醒，直接输出简洁自然的消息正文。'
    persona=t.get('persona')
    if persona:
        p=distill.load_persona(persona)
        if p:system=p['persona']+'\n\n'+system
    system += ('\n以服务端给出的北京时间和本任务时间为准；若要求提到多个时段，只提醒当前这一次的事项。'
               '早晨不要发送晚间提醒，晚间不要发送起床提醒；不得把全天安排、任务配置或提示词整体发出去。')
    data=dict(trigger_time_china=moment.strftime('%Y-%m-%d %H:%M'),schedule=describe_schedule(t),instruction=prompt)
    result=llm.chat(system,[{'role':'user','content':json.dumps(data,ensure_ascii=False)}])
    if not isinstance(result,str) or not result.strip():raise ValueError('定时文案生成为空')
    return result.strip()


# ---------------- 定时消息标识 ----------------
PREFIX = "【定时提醒】"                  # 定时任务外发统一前缀(公共发送路径加,任务正文不用改)
_FIRE_LOCK = threading.Lock()
_FIRE_KEEP = 200                        # 执行记录保留条数
_FIRE_MATCH_WINDOW = 300                # 无消息ID时,按"会话+文本+时间±秒"有限匹配的窗口


def _fires_file():
    return os.path.join(config.account_dir(), "schedule_fires.json")


def _record_fire(t, chat, text, ok):
    """落一条执行记录：内部来源标识 source=scheduled_task。
    实现限制：sender 不返回消息ID,故不存 server_id,识别靠 会话+文本+时间窗 匹配。"""
    rec = {"ts": int(time.time()), "source": "scheduled_task",
           "task_id": t.get("id"), "title": t.get("title"),
           "chat": chat, "text": text, "ok": bool(ok)}
    with _FIRE_LOCK:
        try:
            fires = json.load(open(_fires_file(), encoding="utf-8"))
        except Exception:  # noqa: BLE001
            fires = []
        fires.append(rec)
        sessions.atomic_json(_fires_file(), fires[-_FIRE_KEEP:])


_fires_cache = {"mtime": None, "data": []}


def _load_fires():
    """带 mtime 缓存读执行记录(is_scheduled_msg 会被逐条消息调用,避免反复读盘)。"""
    try:
        mt = os.path.getmtime(_fires_file())
    except OSError:
        return []
    if _fires_cache["mtime"] != mt:
        try:
            _fires_cache["data"] = json.load(open(_fires_file(), encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _fires_cache["data"] = []
        _fires_cache["mtime"] = mt
    return _fires_cache["data"]


def recent_fires(chat=None, limit=10, within=None):
    """查执行记录(新→旧)。chat 限定会话；within 限定最近多少秒。"""
    fires = _load_fires()
    now = time.time()
    out = [f for f in reversed(fires)
           if (not chat or f.get("chat") == chat)
           and (not within or now - f.get("ts", 0) <= within)]
    return out[:limit]


def is_scheduled_msg(msg, chat=None):
    """判断一条聊天消息是否本机器人的定时任务消息(供上下文/记忆/检索排除)。

    只可能命中【自己发出】(is_self)的消息——用户引用/自己输入"【定时提醒】"永不被过滤。
    识别优先级：
    1) 执行记录匹配：同会话 + 文本一致 + 发送时间在 ±{_FIRE_MATCH_WINDOW}s 内(无消息ID的有限匹配,
       同窗口同文本的自发消息会被一并视为定时消息——极端少见,已知限制)。
    2) 兜底：自己发的且以前缀开头(机器人正常回复不会带此前缀)。
    旧的无前缀且无记录的提醒无法识别(不凭正文相同批量猜),保持原样。
    """
    if not msg.get("is_self"):
        return False
    text = (msg.get("content") or "").strip()
    if not text:
        return False
    ct = msg.get("create_time") or 0
    for f in recent_fires(chat=chat or None, limit=_FIRE_KEEP):
        if f.get("text") == text and abs(ct - f.get("ts", 0)) <= _FIRE_MATCH_WINDOW:
            return True
    return text.startswith(PREFIX)


def fire(t, occurrence=None, session=None):
    """One durable execution per account/task/due time. Manual run gets a new ID."""
    from core import sender
    token = session or sessions.capture()
    occurrence = occurrence or ('manual:' + send_ledger.next_id())
    task_identity = t.get('send_uid') or ('legacy', t.get('id'), t.get('created'))
    jid = send_ledger.stable_id(token['account'], 'schedule', task_identity, occurrence)
    ledger = send_ledger.Ledger()
    child_id = send_ledger.stable_id(jid, 'send')
    prior = ledger.get(jid)
    if prior:
        if prior['status'] == 'not_sent' and prior['reason'] == 'open_failed':
            result = sender.retry(child_id, session=token)
            return ledger.view(ledger.update(jid, result['status'], result['reason']))
        # working includes a crash during generation; hold for review, never replay.
        return ledger.view(prior)
    ledger.prepare(jid, token, t.get('target_username'), 'schedule_execution')
    if not sessions.valid(token):
        return ledger.view(ledger.update(jid, 'stale', 'account_session_changed'))
    blocked = sender.preflight(t.get('target_username'), session=token)
    if blocked:
        return ledger.view(ledger.update(jid, blocked['status'], blocked['reason']))
    if not ledger.begin_work(jid):
        return ledger.view(ledger.get(jid))
    try:
        with sessions.bind(token):
            trigger_at=time.time()
            if occurrence.startswith('cron:'):
                try:trigger_at=int(occurrence.split(':',1)[1])*60
                except ValueError:pass
            try:
                text = _compute_text(dict(t,_trigger_at=trigger_at))
            except sessions.StaleAccount:raise
            except Exception:
                _log('定时文案生成失败，未发送原始任务描述')
                return ledger.view(ledger.update(jid,'not_sent','generation_failed'))
            sessions.check(token)
            if not text or not t.get('target_username'):
                return ledger.view(ledger.update(jid, 'not_sent', 'missing_content_or_target'))
            if not text.startswith(PREFIX):
                text = PREFIX + text
            uname = t['target_username']
            disp = t.get('target_display') or t.get('target')
            child_id = send_ledger.stable_id(jid, 'send')
            if t.get('is_group') and t.get('mention_wxid'):
                r = sender.send_at(disp, uname, t.get('mention_wxid'),
                    t.get('mention_display') or '', text, job_id=child_id, session=token)
            else:
                r = sender.send_text(disp, text, chat_username=uname,
                                     job_id=child_id, session=token)
            # Commit authoritative outcome BEFORE legacy history; that history is not
            # the idempotency mechanism and its failure must never repeat a send.
            row = ledger.update(jid, r.get('status', 'uncertain'), r.get('reason', 'send_result'))
            if row['status'] in ('confirmed', 'submitted'):
                try:
                    sessions.check(token)
                    _record_fire(t, uname, text, True)
                except Exception as exc:
                    _log('execution confirmed; legacy record failed: ' + type(exc).__name__)
            return ledger.view(row)
    except sessions.StaleAccount:
        return ledger.view(ledger.update(jid, 'stale', 'account_session_changed'))
    except Exception as exc:
        # A committed child may have sent. Preserve conservative review state.
        return ledger.view(ledger.update(jid, 'uncertain', 'execution_exception_' + type(exc).__name__))


@sessions.task
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
                occurrence = ('once:' + t['once_at']) if t.get('once_at') else ('cron:' + str(cur_min))
                r = fire(t, occurrence=occurrence, session=sessions.capture())
                sessions.check()
                if r.get('status') in ('confirmed', 'submitted'):
                    t['last_min'] = cur_min
                    t['last_fired'] = time.strftime('%Y-%m-%d %H:%M', tm)
                    changed = True
                    if t.get('once_at'):
                        keep = False
                # All unsuccessful/uncertain once tasks remain. SQLite prevents
                # repeating this occurrence, even if the JSON save below fails.
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
             use_llm=False, persona=None, creator_wxid=None, creator_name=None):
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
    t = {"id": _new_id(tasks), "send_uid": uuid.uuid4().hex, "title": title or (prompt or "定时任务")[:16],
         "target": target, "target_username": uname, "target_display": disp,
         "is_group": bool(is_group), "mention": mention, "mention_wxid": mention_wxid,
         "mention_display": mention_disp, "cron": cron, "once_at": once_at,
         "prompt": prompt, "use_llm": bool(use_llm), "persona": persona,
         "creator_wxid": creator_wxid, "creator_name": creator_name,
         "enabled": True, "created": time.strftime("%Y-%m-%d %H:%M")}
    tasks.append(t)
    save_tasks(tasks)
    _log(f"新建任务[{t['title']}] -> {disp} @{describe_schedule(t)}")
    return t, None


_EDITABLE = ("title", "prompt", "cron", "once_at", "use_llm", "target", "mention", "persona")


def update_task(id, fields, creator_wxid=None):
    """编辑已有任务。fields 只取白名单字段;改 target/mention 会重新解析会话/成员。
    creator_wxid 非空时只能改自己建的(数据隔离)。返回 (task, error)。"""
    tasks = load_tasks()
    t = next((x for x in tasks if x.get("id") == id), None)
    if not t:
        return None, "找不到该任务"
    if creator_wxid is not None and t.get("creator_wxid") != creator_wxid:
        return None, "只能编辑你自己设置的任务"
    upd = {k: fields[k] for k in _EDITABLE if k in fields}
    # 目标改了→重新解析会话
    if "target" in upd:
        tgt = (upd["target"] or "").strip()
        uname, is_group, disp = resolve_target(tgt)
        if not uname:
            return None, f"找不到会话/联系人：{tgt}"
        t.update(target=tgt, target_username=uname, target_display=disp,
                 is_group=bool(is_group))
    # 决定最终的 cron/once_at(允许其一为空,但不能都空)
    cron = (upd["cron"] if "cron" in upd else t.get("cron")) or None
    once_at = (upd["once_at"] if "once_at" in upd else t.get("once_at")) or None
    if isinstance(cron, str):
        cron = cron.strip() or None
    if isinstance(once_at, str):
        once_at = once_at.strip() or None
    if cron and once_at:                  # 改成周期性就清掉一次性时间,反之亦然
        if "cron" in upd:
            once_at = None
        elif "once_at" in upd:
            cron = None
    if not cron and not once_at:
        return None, "没有解析到时间(cron 或 once_at)"
    if cron and not _valid_cron(cron):
        return None, (f"cron 不合法：{cron}。字段顺序是「分 时 日 月 周」"
                      "(分0-59 时0-23 日1-31 月1-12 周0-7)，不用的位填 *。"
                      "例:每周一17点=0 17 * * 1；周三到五18点=0 18 * * 3-5")
    if once_at:
        try:
            time.strptime(once_at, "%Y-%m-%d %H:%M")
        except Exception:
            return None, f"时间格式不对：{once_at}（需 YYYY-MM-DD HH:MM）"
    t["cron"], t["once_at"] = cron, once_at
    # 群里@成员重新解析
    if "mention" in upd:
        mention = (upd["mention"] or "").strip() or None
        t["mention"] = mention
        t["mention_wxid"] = t["mention_display"] = None
        if t.get("is_group") and mention:
            mw, md = resolve_member(t["target_username"], mention)
            if not mw:
                return None, f"群里找不到成员：{mention}"
            t["mention_wxid"], t["mention_display"] = mw, md
    for k in ("title", "prompt", "persona"):
        if k in upd:
            t[k] = (upd[k] or "").strip() or (t.get(k) if k != "prompt" else "")
    if "use_llm" in upd:
        t["use_llm"] = bool(upd["use_llm"])
    if "fired" in t:                      # 一次性任务改了时间→允许再次触发
        t.pop("fired", None)
    save_tasks(tasks)
    _log(f"编辑任务[{t['title']}] -> {t['target_display']} @{describe_schedule(t)}")
    return t, None


_CRON_RANGE = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]  # 分 时 日 月 周


def _valid_cron(cron):
    """5 字段且每个字段的每个数值都在合法范围内(否则会得到永不触发的 cron)。
    支持 * / a-b / */n / 逗号列表。"""
    parts = str(cron).split()
    if len(parts) != 5:
        return False
    for expr, (lo, hi) in zip(parts, _CRON_RANGE):
        for token in expr.split(","):
            token = token.strip()
            if token in ("*", ""):
                continue
            m = re.fullmatch(r"\*/(\d+)", token)         # */n 步长
            if m:
                if int(m.group(1)) >= 1:
                    continue
                return False
            m = re.fullmatch(r"(\d+)-(\d+)", token)      # a-b 范围
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                if lo <= a <= b <= hi:
                    continue
                return False
            if token.isdigit() and lo <= int(token) <= hi:  # 单值
                continue
            return False
    return True


_GENERIC_WORDS = ("定时任务", "任务", "提醒我", "提醒", "定时", "闹钟", "通知", "一下", "的", "取消", "删除")


def _strip_generic(s):
    for w in _GENERIC_WORDS:
        s = s.replace(_norm(w), "")
    return s


def remove_task(id=None, match=None, creator_wxid=None):
    """按 id 或 match 删除。creator_wxid 非空时【只在该创建者自己的任务里】删(数据隔离)。"""
    tasks = load_tasks()
    keep, removed = [], 0
    for t in tasks:
        if creator_wxid is not None and t.get("creator_wxid") != creator_wxid:
            keep.append(t)                    # 不是你建的,跳过(隔离:不能删别人的)
            continue
        hit = False
        if id is not None and t.get("id") == id:
            hit = True
        elif match:
            m = _norm(match)
            hay = _norm(t.get("title")) + " " + _norm(t.get("target")) + " " + _norm(t.get("prompt"))
            # 去掉"任务/提醒/定时…"这类通用词再比,避免"取消X任务"命中所有含"任务"的
            mc, hc = _strip_generic(m), _strip_generic(hay)
            if m and (m in hay or (mc and (mc in hc
                      # 宽松:去通用词后的实义 2-gram 命中(容忍"周报提醒"↔"提醒交周报"词序)
                      or any(mc[i:i+2] in hc for i in range(len(mc) - 1) if len(mc) >= 2)))):
                hit = True
        if hit:
            removed += 1
        else:
            keep.append(t)
    if removed:
        save_tasks(keep)
    return removed


def find_tasks(match, creator_wxid=None):
    """按关键词找任务(匹配规则与 remove_task 一致),不改动任何东西。"""
    out = []
    m = _norm(match or "")
    if not m:
        return out
    mc = _strip_generic(m)
    for t in load_tasks():
        if creator_wxid is not None and t.get("creator_wxid") != creator_wxid:
            continue
        hay = _norm(t.get("title")) + " " + _norm(t.get("target")) + " " + _norm(t.get("prompt"))
        hc = _strip_generic(hay)
        if m in hay or (mc and (mc in hc
                or any(mc[i:i+2] in hc for i in range(len(mc) - 1) if len(mc) >= 2))):
            out.append(t)
    return out


# 对"刚收到的提醒"的典型回应(不用提醒了/改成六点…)——looks_schedule 抓不到这类短句,
# 只有当该会话【最近确实触发过提醒】时才按此路由到定时处理,平时不干扰普通聊天
_REMIND_RESP = re.compile(
    r"不用(再)?提醒|别(再)?提醒|取消(这个|那个|刚才)?(的)?提醒|停掉(这个)?提醒|"
    r"改成|改到|改一下时间|时间改一?下|提前.{0,4}(分钟|小时|点)|推迟|延后|换个时间")


def looks_reminder_response(text):
    return bool(text and _REMIND_RESP.search(text))


def list_view(creator_wxid=None):
    """列出任务。creator_wxid 非空时【只列该创建者自己的】(数据隔离);为空=全部(主人视角)。"""
    out = []
    for t in load_tasks():
        if creator_wxid is not None and t.get("creator_wxid") != creator_wxid:
            continue
        out.append({**t, "schedule_desc": describe_schedule(t), "next": _next_hint(t)})
    return out


# ---------------- 自然语言 ----------------
_NL_SYSTEM = """你把用户对"定时任务"的自然语言指令解析成 JSON。任务到点会把结果发给某人:私聊直发,群聊@他。
严格只输出 JSON,字段:
{
  "action": "create|cancel|update|list|fired|ask|none",  // 新建/取消/改时间/列任务/查最近提醒了什么/需要反问澄清; none=这根本不是定时任务指令(只是普通聊天)
  "title": "简短标题(<=16字)",
  "target": "发给谁 或 哪个群——【原样完整照抄名字,含数字/连字符,如 test-001 不要写成 test】",
  "mention": "群里要@的人名(原样完整照抄;私聊或无需@则空串)",
  "cron": "分 时 日 月 周 (标准5字段cron)，非周期性任务给空串",
  "tasks": [{"title":"早安提醒","cron":"30 6 * * *","once_at":"","prompt":"早上好，该起床啦，新的一天开始了～","use_llm":false}], // 不同时间有不同内容时必填，每项独立保存时间、正文和是否使用AI；这种情况顶层 cron/crons/once_at/prompt 留空
  "crons": ["多条cron"],  // 当一条cron表达不了(不同星期几用不同钟点)时,拆成多条放这里;单一时间点就用上面的cron、这里给空数组[]
  "once_at": "YYYY-MM-DD HH:MM (一次性任务的绝对时间)，周期性给空串",
  "prompt": "到点要做/要说的内容",
  "use_llm": true/false,              // 内容需要AI临时生成(如"总结新闻""随便说点什么")=true; 固定话术(如"提醒交周报")=false
  "persona": "指定用谁的口吻/人设说(如指令提到'让夏以昼说',填 夏以昼;否则空串)",
  "cancel_match": "取消/删除/改时间时用来匹配已有任务的关键词(action=cancel|update时给)",
  "ask": "action=ask时,要反问用户的澄清问题"
}
规则:
- 【时间与内容必须对应】例如“早上六点半叫我起床，晚上九点半提醒刷牙洗漱”必须返回 tasks 两项：06:30 只含起床文案，21:30 只含洗漱文案。不能给两项复制完整原始指令，不能只用 crons 配一个共用 prompt。不同分钟也分别配对，避免 cron 的分/时列表形成额外组合。固定温柔提醒可直接写好正文，use_llm=false。
- 只有所有时刻提醒同一件事（如多次喝水）才允许 crons/cron 共用 prompt。tasks 最多20项，目标与被@人统一沿用顶层 target/mention；不要在任务正文中混入其他时段的事项。
- 上下文可能给出【最近触发的提醒】。用户对刚收到的提醒说"不用提醒了/别再提醒/取消这个"→action=cancel,cancel_match填该提醒对应的任务标题;说"改成六点/提前十分钟"这类改时间→action=update,cancel_match填任务标题,cron/once_at 填改后的新时间(基于原任务时间推算)。
- 【保守】指代不明(最近提醒有多条不同任务、或根本没有最近提醒且没说清是哪条)时→action=ask,在 ask 里问清楚是哪条任务,绝不猜。
- 用户问"刚才提醒了什么/最近提醒过什么"→action=fired。
- 文本开头可能有"@机器人自己"的前缀(如"@Giut.nik ..."),【忽略它】,它不是要提醒的对象。
- 指令里明确写"@某人提醒..."的那个"某人"才是 mention(要@提醒的对象),原样照抄他的名字。
- "每天9点"→cron "0 9 * * *"; "每周一10:30"→"30 10 * * 1"; "每小时"→"0 * * * *"; "每30分钟"→"*/30 * * * *"。
- 【多时间点】不同星期几用不同钟点时,一条cron表达不了,必须拆进 crons 数组。例:"周一下午5点、周二下午4点、周三到周五下午6点"→crons=["0 17 * * 1","0 16 * * 2","0 18 * * 3-5"],此时 cron 给空串。
- "明天下午3点"这种一次性→用 once_at 绝对时间(基于下面给的当前时间推算)。
- "每隔一小时随便找我说点什么"这种→周期任务:cron "0 */1 * * *",use_llm=true,prompt照抄要求。
- 只有取消意图→action=cancel,填 cancel_match;只有查看意图→action=list。
- 如果这句话【不是】在设置/取消/查看定时任务(只是普通闲聊)→action=none,其它字段可空。
- 不确定的字段给空串。不要输出 JSON 以外任何字。"""


def parse_nl(text, extra=""):
    now = time.strftime("%Y-%m-%d %H:%M %A")
    raw = llm.chat(_NL_SYSTEM, [{"role": "user",
                   "content": f"当前时间:{now}\n{extra}指令:{text}"}])
    raw = raw.replace("```json", "").replace("```", "")
    raw = raw[raw.find("{"): raw.rfind("}") + 1]
    return json.loads(raw)


_SCHED_VERB = re.compile(r"提醒|定时|闹钟|催我|叫我|通知我|找我|发我|跟我说|给我说|说点什么|唠")
_SCHED_TIME = re.compile(
    r"每[天周月日]|每隔|每小时|每分钟|每\d|明天|后天|今天|上午|下午|晚上|早上|凌晨|"
    r"周[一二三四五六日天]|礼拜|\d+\s*点|\d{1,2}:\d{2}|过\d+分|一分钟|半小时")
# 周期时间(每隔/每天/每小时…)单独出现就足以怀疑是定时任务,交给 LLM 兜底判定
_SCHED_RECUR = re.compile(r"每隔|每小时|每分钟|每天|每周|每月|每\d+\s*(分钟|小时|天)|每[一二三四五六日天]")
_SCHED_MANAGE = re.compile(r"(取消|删除|关掉|停掉|列出|查看|有哪些|看看).{0,8}(任务|提醒|定时|闹钟)"
                           r"|提醒(了|过)(什么|啥)|(刚才|最近).{0,4}提醒")


def looks_schedule(text):
    """粗判一句话是不是"定时任务"候选(机器人聊天里初筛;最终由 LLM 判定,见 parse_nl 的 none)。"""
    if not text:
        return False
    if _SCHED_MANAGE.search(text) or _SCHED_RECUR.search(text):
        return True
    return bool(_SCHED_VERB.search(text) and _SCHED_TIME.search(text))


_SELF_WORDS = {"我", "自己", "me", "本人", "俺"}


@sessions.task
def handle_nl(text, ctx=None):
    """解析并执行一条自然语言定时指令。返回 {ok, action, message, ...}。
    ctx(可选)={chat_username, chat_display, is_group, requester_wxid, requester_name}：
    指令来自聊天时用作默认目标——说"提醒我"就发回给说话的人(私聊直发,群里@他)。"""
    if not llm.available():
        return {"ok": False, "message": "未配置 LLM,无法解析自然语言"}
    # 该会话最近触发过的提醒 → 给解析器做指代消解("不用提醒了"="取消刚才那条对应的任务")
    extra = ""
    if ctx and ctx.get("chat_username"):
        rf = recent_fires(chat=ctx["chat_username"], limit=3, within=3600)
        if rf:
            extra = "最近触发的提醒(新→旧):\n" + "\n".join(
                f"- 任务「{f.get('title')}」(task_id={f.get('task_id')}) "
                f"于{time.strftime('%H:%M', time.localtime(f.get('ts', 0)))}发送:"
                f"{(f.get('text') or '')[:40]}" for f in rf) + "\n"
    try:
        d = parse_nl(text, extra=extra)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"解析失败: {e}"}
    sessions.check()
    # 数据隔离：来自聊天(ctx 有 requester_wxid)时,只能看/删自己建的;网页(无ctx)=主人看全部
    creator = ctx.get("requester_wxid") if ctx else None
    action = (d.get("action") or "create").lower()
    if action == "none":                 # LLM 判定这不是定时指令→交回普通聊天处理
        return {"ok": False, "action": "none", "message": "(非定时任务指令)"}
    if action == "ask":                  # 指代不明→反问,绝不乱取消/乱改
        return {"ok": True, "action": "ask",
                "message": d.get("ask") or "你说的是哪条提醒任务?"}
    if action == "fired":                # 查最近提醒了什么(只给本会话收到的,遵守会话边界)
        rf = recent_fires(chat=(ctx or {}).get("chat_username"), limit=8)
        if not rf:
            return {"ok": True, "action": "fired", "message": "这个会话最近没有触发过定时提醒"}
        msg = "最近的定时提醒记录：\n" + "\n".join(
            f"· {time.strftime('%m-%d %H:%M', time.localtime(f.get('ts', 0)))} "
            f"「{f.get('title')}」{(f.get('text') or '')[:36]}"
            f"{'' if f.get('ok') else '(发送失败)'}" for f in rf)
        return {"ok": True, "action": "fired", "message": msg}
    if action == "list":
        return {"ok": True, "action": "list", "tasks": list_view(creator_wxid=creator)}
    if action == "cancel":
        n = remove_task(match=d.get("cancel_match") or d.get("target") or d.get("title"),
                        creator_wxid=creator)
        return {"ok": n > 0, "action": "cancel",
                "message": f"已取消 {n} 个任务" if n else "没找到你设置的匹配任务"}
    if action == "update":               # 改时间：唯一匹配才改,多条/没有→反问
        match = d.get("cancel_match") or d.get("title") or ""
        hits = find_tasks(match, creator_wxid=creator)
        if not hits:
            return {"ok": False, "action": "ask",
                    "message": f"没找到匹配「{match}」的任务,你说的是哪条?"}
        if len(hits) > 1:
            names = "、".join(f"「{t['title']}({describe_schedule(t)})」" for t in hits[:4])
            return {"ok": False, "action": "ask",
                    "message": f"匹配到多条:{names},要改哪一条?"}
        fields = {}
        if (d.get("cron") or "").strip():
            fields["cron"] = d["cron"].strip()
        if (d.get("once_at") or "").strip():
            fields["once_at"] = d["once_at"].strip()
        if not fields:
            return {"ok": False, "action": "ask",
                    "message": "要改成什么时间?(没解析到新时间)"}
        t2, err = update_task(hits[0]["id"], fields, creator_wxid=creator)
        if err:
            return {"ok": False, "action": "update", "message": err}
        return {"ok": True, "action": "update",
                "message": f"已改:「{t2['title']}」{describe_schedule(t2)}, 下次 {_next_hint(t2)}"}
    # create——目标为空或"我/自己"时,用指令来源(说话的人/所在会话)作默认目标
    tgt = (d.get("target") or "").strip()
    mention = (d.get("mention") or "").strip() or None
    if ctx and (not tgt or _norm(tgt) in {_norm(w) for w in _SELF_WORDS}):
        if ctx.get("is_group"):
            tgt = ctx.get("chat_display") or ctx.get("chat_username")
            mention = mention or ctx.get("requester_name")   # 群里默认@提出人
        else:
            tgt = ctx.get("chat_display") or ctx.get("chat_username")
    # Per-time content is authoritative; never merge it into one shared prompt.
    entries=d.get('tasks')
    if entries:
        if not isinstance(entries,list) or len(entries)>20:
            return dict(ok=False,action='create',message='独立提醒格式无效，最多20条')
        prepared=[]
        for entry in entries:
            if not isinstance(entry,dict):return dict(ok=False,action='create',message='独立提醒格式无效')
            cron=entry.get('cron') or None;once=entry.get('once_at') or None;body=entry.get('prompt')
            if bool(cron)==bool(once) or (cron and not _valid_cron(cron)) or not isinstance(body,str) or not body.strip():
                return dict(ok=False,action='create',message='每条提醒需要有效的独立时间和对应内容')
            if once:
                try:datetime.strptime(once,'%Y-%m-%d %H:%M')
                except (ValueError,TypeError):return dict(ok=False,action='create',message='一次性提醒时间格式无效')
            if type(entry.get('use_llm',False)) is not bool:
                return dict(ok=False,action='create',message='文案生成开关格式无效')
            prepared.append(dict(title=entry.get('title') or d.get('title'),target=tgt,prompt=body.strip(),cron=cron,once_at=once,
                mention=mention,use_llm=entry.get('use_llm',False),persona=d.get('persona') or None,
                creator_wxid=creator,creator_name=(ctx.get('requester_name') if ctx else None)))
        made=[]
        for args in prepared:
            task,error=add_task(**args)
            if error:return dict(ok=False,action='create',message=error,tasks=made)
            made.append(task)
        return dict(ok=True,action='create',tasks=made,message='已分别创建 '+str(len(made))+' 条提醒：'+
            '；'.join(t['title']+'（'+describe_schedule(t)+'）' for t in made))
    # 多时间点：不同星期几用不同钟点,一条 cron 表达不了 → crons 里每条各建一个任务
    crons = [c.strip() for c in (d.get("crons") or []) if isinstance(c, str) and c.strip()]
    single = (d.get("cron") or "").strip()
    if single and single not in crons:
        crons.insert(0, single)
    common = dict(
        title=d.get("title"), target=tgt, prompt=d.get("prompt"),
        once_at=(d.get("once_at") or "").strip() or None,
        mention=mention, use_llm=bool(d.get("use_llm")),
        persona=(d.get("persona") or "").strip() or None,
        creator_wxid=creator,
        creator_name=(ctx.get("requester_name") if ctx else None))
    if len(crons) > 1:
        made, errs = [], []
        for c in crons:
            t, err = add_task(cron=c, **common)
            (errs if err else made).append(err or t)
        if not made:
            return {"ok": False, "action": "create",
                    "message": errs[0] if errs else "创建失败", "parsed": d}
        disp0 = made[0]["target_display"]
        descs = "；".join(describe_schedule(t) for t in made)
        msg = f"已创建 {len(made)} 条:「{made[0]['title']}」→ {disp0}（{descs}）"
        if errs:
            msg += f"；{len(errs)} 条失败：{errs[0]}"
        return {"ok": True, "action": "create", "message": msg, "tasks": made}
    t, err = add_task(cron=(crons[0] if crons else None), **common)
    if err:
        return {"ok": False, "action": "create", "message": err, "parsed": d}
    return {"ok": True, "action": "create",
            "message": f"已创建:「{t['title']}」→ {t['target_display']} "
                       f"({describe_schedule(t)}, 下次 {_next_hint(t)})", "task": t}
