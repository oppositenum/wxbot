"""长期记忆 / 人物画像：从对话里增量抽取每个人的稳定事实，累积存档，回复时【按需检索】注入。

每人一份 accounts/<wxid>/profiles/<对方wxid>.json：
  {wxid, name, facts:[<fact>], summary, updated}

fact 生命周期字段(v2，向后兼容旧的 {fact, ts})：
  id           稳定 ID
  text         事实正文
  type         identity|preference|plan|event|relationship|emotion|unknown
  source_msg_id 来源消息的 server_id(可空)
  recorded_ts  记录时间(写入时间)——旧数据的 ts 只能解释为记录时间
  event_ts     事件发生时间(未知=None，绝不拿 recorded_ts 冒充)
  status       active|cancelled|superseded
  scope        作用域，如 "chat:<对方wxid>" / "group:<roomid>"；私聊记忆默认不在群里公开
  expires_ts   有效期(可空)——情绪类短期失效
  confidence   high|low|unknown
  superseded_by 被哪条新事实取代(可空)

关键原则：
- 每轮【不再全量注入】。select_memories() 按当前消息+近期话题检索，最多 N 条(默认3)，允许返回 0 条，无命中不拿 summary 兜底。
- 抽取只收稳定、有长期价值的信息；忽略好友验证等系统通知；不把"我方"的话抽成对方事实；不把人设虚构经历写进真实记忆；临时情绪短期失效。
- 更正/取消需要来源支持；同主题不等于冲突(喜欢川菜和粤菜可并存)；冲突不确定就都留、标低置信，不擅自选一个当真。
"""
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from core import llm  # noqa: E402

MAX_FACTS = 40             # 超过就触发压缩
DEFAULT_BUDGET = 3         # 每轮最多入选的记忆条数(可被 cfg.memory_budget 覆盖)
EMOTION_TTL = 3 * 86400    # 情绪类事实的默认有效期(秒)
FACT_TYPES = ("identity", "preference", "plan", "event",
              "relationship", "emotion", "unknown")

# 明显无价值/系统通知类，绝不作为长期事实
_JUNK_RE = re.compile(
    r"好友验证|通过.*验证|添加.*好友|成为.*好友|加了.*好友|是.*微信好友|微信好友$|"
    r"对方已通过|你已添加|现在可以开始聊天|拍了拍|撤回了一条消息|开启了朋友验证")


from core import account_session as sessions

def scope_of_chat(chat_username):
    """会话→记忆作用域：群=group:<房间>，私聊=chat:<对方wxid>。"""
    cu = chat_username or "unknown"
    if str(cu).endswith("@chatroom"):
        return "group:" + cu
    return "chat:" + cu


def _dir():
    d = os.path.join(config.account_dir(), "profiles")
    os.makedirs(d, exist_ok=True)
    return d


def _path(wxid):
    safe = (wxid or "unknown").replace("/", "_").replace("@", "_at_")
    return os.path.join(_dir(), safe + ".json")


def _gen_id(text):
    return "f_" + hashlib.md5((text or "").encode("utf-8")).hexdigest()[:10]


def _migrate_fact(f, wxid):
    """把任意一条 fact 归一到 v2 结构(在内存里，不落盘)。兼容旧的 {fact, ts}。"""
    if not isinstance(f, dict):
        f = {"text": str(f)}
    text = (f.get("text") or f.get("fact") or "").strip()
    scope = f.get("scope") or ("chat:" + (wxid or "unknown"))
    return {
        "id": f.get("id") or _gen_id(text),
        "text": text,
        "type": f.get("type") if f.get("type") in FACT_TYPES else "unknown",
        "source_msg_id": f.get("source_msg_id"),
        # 旧数据只有 ts：解释为【记录时间】，绝不当事件时间
        "recorded_ts": f.get("recorded_ts") or f.get("ts") or 0,
        "event_ts": f.get("event_ts"),          # 未知就是 None
        "status": f.get("status") or "active",
        "scope": scope,
        "expires_ts": f.get("expires_ts"),
        "confidence": f.get("confidence") or "unknown",
        "superseded_by": f.get("superseded_by"),
    }


def load_profile(wxid):
    try:
        p = json.load(open(_path(wxid), encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"wxid": wxid, "name": "", "facts": [], "summary": "", "updated": 0}
    p["facts"] = [_migrate_fact(f, wxid) for f in p.get("facts", [])]
    p.setdefault("style", {})
    p.setdefault("emotion_history", [])
    return p


def save_profile(p):
    p["updated"] = int(time.time())
    sessions.atomic_json(_path(p["wxid"]), p)


def update_style_from_messages(msgs, scope=None):
    """无模型地累计表达习惯与近期情绪线索，失败不影响消息处理。

    只保存统计和短标签，不保存整段聊天文本；style_by_scope 避免把群聊
    语气误套到私聊。情绪线索有时间戳，调用方展示时只取近期记录。
    """
    by_user = {}
    for m in msgs or []:
        if m.get("is_self") or not m.get("sender"):
            continue
        text = (m.get("content") or "").strip()
        if not text or text.startswith("[") or _JUNK_RE.search(text):
            continue
        by_user.setdefault(m["sender"], {"name": m.get("sender_name") or m["sender"], "texts": []})["texts"].append(text)
    now = int(time.time())
    for wxid, item in by_user.items():
        p = load_profile(wxid)
        if item["name"] and not p.get("name"):
            p["name"] = item["name"]
        styles = p.setdefault("style_by_scope", {})
        key = scope or ("chat:" + str(wxid))
        s = styles.setdefault(key, {"messages": 0, "chars": 0, "exclamations": 0,
                                    "questions": 0, "emoji": 0, "ellipsis": 0,
                                    "short_replies": 0, "updated": now})
        for text in item["texts"]:
            s["messages"] += 1; s["chars"] += len(text)
            s["exclamations"] += text.count("!") + text.count("！")
            s["questions"] += text.count("?") + text.count("？")
            s["emoji"] += sum(1 for c in text if ord(c) > 0x1F000)
            s["ellipsis"] += text.count("…") + text.count("...")
            if len(text) <= 12: s["short_replies"] += 1
        s["updated"] = now
        # 可解释的近期情绪标签，仅作弱背景，避免把一次用词当人格定论。
        mood = None
        if any(x in " ".join(item["texts"]) for x in ("开心", "哈哈", "太好了", "开心死了")): mood = "愉快"
        elif any(x in " ".join(item["texts"]) for x in ("难过", "烦死", "生气", "郁闷", "崩溃")): mood = "低落或烦恼"
        elif any(x in " ".join(item["texts"]) for x in ("累", "困", "疲惫")): mood = "疲惫"
        if mood:
            hist = p.setdefault("emotion_history", [])
            hist.append({"label": mood, "ts": now, "scope": key})
            p["emotion_history"] = hist[-20:]
        save_profile(p)


def style_context(wxid, scope=None, max_age=30 * 86400):
    """返回可注入模型的简短风格/近期情绪背景；没有数据返回空串。"""
    p = load_profile(wxid); s = (p.get("style_by_scope") or {}).get(scope or ("chat:" + str(wxid)))
    if not s or not s.get("messages"): return ""
    n = s["messages"]; avg = s["chars"] / max(1, n)
    bits = [f"平均每条约{avg:.0f}字"]
    if s["short_replies"] / n >= .6: bits.append("常用短句")
    if s["exclamations"] / n >= .35: bits.append("感叹号较多")
    if s["questions"] / n >= .35: bits.append("经常用问句")
    if s["emoji"]: bits.append("会使用表情")
    if s["ellipsis"] / n >= .2: bits.append("常用省略号")
    moods = [e["label"] for e in p.get("emotion_history", []) if e.get("scope") == (scope or ("chat:" + str(wxid))) and time.time() - e.get("ts", 0) < max_age]
    if moods: bits.append("近期情绪线索：" + moods[-1])
    return "【表达习惯背景（弱参考）】" + "、".join(bits)


def list_profiles():
    out = []
    for fn in sorted(os.listdir(_dir())) if os.path.isdir(_dir()) else []:
        if fn.endswith(".json"):
            try:
                p = json.load(open(os.path.join(_dir(), fn), encoding="utf-8"))
                active = sum(1 for f in p.get("facts", [])
                             if isinstance(f, dict) and f.get("status", "active") == "active")
                out.append({"wxid": p.get("wxid"), "name": p.get("name"),
                            "facts": active,
                            "summary": p.get("summary", ""),
                            "updated": p.get("updated", 0)})
            except Exception:  # noqa: BLE001
                pass
    return out


@sessions.task
def _add_facts(wxid, name, facts, scope=None, source_msg_id=None):
    """facts: [{text,type,event_ts?} | str]。合并进画像：文本全等去重；
    情绪类给短有效期；不覆盖同主题旧事实(只增不改，冲突交给检索期处理)。"""
    facts = [f for f in facts if f]
    if not facts:
        return
    p = load_profile(wxid)
    if name and not p.get("name"):
        p["name"] = name
    scope = scope or ("chat:" + (wxid or "unknown"))
    have = {f["text"] for f in p.get("facts", [])}
    now = int(time.time())
    for f in facts:
        if isinstance(f, str):
            f = {"text": f}
        text = (f.get("text") or "").strip()
        if not text or text in have or _JUNK_RE.search(text):
            continue
        ftype = f.get("type") if f.get("type") in FACT_TYPES else "unknown"
        rec = {
            "id": _gen_id(text + str(now)), "text": text, "type": ftype,
            "source_msg_id": f.get("source_msg_id") or source_msg_id,
            "recorded_ts": now, "event_ts": f.get("event_ts"),
            "status": "active", "scope": scope,
            "expires_ts": (now + EMOTION_TTL) if ftype == "emotion" else None,
            "confidence": f.get("confidence") or "unknown",
            "superseded_by": None,
        }
        p.setdefault("facts", []).append(rec)
        have.add(text)
    save_profile(p)
    if len([f for f in p.get("facts", []) if f.get("status") == "active"]) > MAX_FACTS:
        compress(wxid)


@sessions.task
def extract_from_messages(msgs, me=None, cfg=None, chat_scope=None):
    """从一批消息里抽取"关于各发言人的稳定事实"，合并进各自画像。返回更新的人数。

    规则(见模块 docstring)：忽略寒暄/系统通知；不抽"我方"的话；情绪单独标类型(短期失效)。
    """
    if not llm.available():
        return 0
    name2wxid = {}
    lines = []
    for m in msgs:
        if m.get("is_self"):            # 不把机器人/我方自己的话抽成对方事实
            continue
        wid = m.get("sender")
        nm = m.get("sender_name") or wid or "对方"
        if wid:
            name2wxid[nm] = wid
        c = (m.get("content") or "").strip()
        if c and not c.startswith("[") and not _JUNK_RE.search(c):
            lines.append(f"{nm}: {c}")
    if len(lines) < 3:
        return 0
    transcript = "\n".join(lines[-60:])
    # 附上各发言人现有 active 事实(带ID)，让同一次抽取顺带识别"更正/取消"
    existing_block = ""
    for nm, wid in name2wxid.items():
        prof = load_profile(wid)
        acts = [f for f in prof.get("facts", []) if f.get("status") == "active"
                and not _JUNK_RE.search(f.get("text") or "")][:20]
        if acts:
            existing_block += f"\n{nm} 的已记事实：\n" + "\n".join(
                f"  [{f['id']}] {f['text']}" for f in acts)
    src_id = next((m.get("server_id") for m in reversed(msgs)
                   if m.get("server_id")), None)
    prompt = (
        "下面是一段微信聊天记录。为其中每个【发言人】抽取关于TA本人的、稳定且有长期价值的事实；"
        "并对照TA的已记事实，识别聊天里明确表达的【更正/取消】。\n"
        "规则：\n"
        "- 只抽身份/职业/居住地/家庭/长期偏好/在做的项目/与我的关系等长期信息。\n"
        "- 忽略寒暄、系统通知(如'已通过好友验证')、一次性内容。\n"
        "- 临时情绪(如'今天很累')可抽但 type 标 emotion；计划标 plan(计划≠已完成)。\n"
        "- 不要臆造聊天里没有的信息；不确定就不写。\n"
        "- 【更正/取消·必须保守】只有当聊天内容能明确对应到某条已记事实(引用其ID)时才输出：\n"
        "  取消(如'不去了'且上下文能确定指哪个计划)→{\"op\":\"cancel\",\"id\":\"..\"}；\n"
        "  改口(如'我搬到上海了'对应旧'住在北京')→{\"op\":\"supersede\",\"id\":\"..\",\"new_text\":\"..\",\"type\":\"..\"}。\n"
        "  指代不明、可能指别的事、或没有对应已记事实时，一律【不要】输出更新。喜欢A又喜欢B不算冲突，两条并存。\n"
        "严格输出 JSON：{\"facts\":{\"发言人\":[{\"text\":\"..\",\"type\":\"identity|preference|plan|event|relationship|emotion|unknown\"}]},"
        "\"updates\":{\"发言人\":[{\"op\":\"cancel|supersede\",\"id\":\"..\",\"new_text\":\"..\"}]}}；"
        "没有就给空对象。不要多余文字。\n"
        + (existing_block + "\n" if existing_block else "")
        + "\n聊天记录：\n" + transcript)
    try:
        raw = llm.chat("你是信息抽取器，只输出 JSON。",
                       [{"role": "user", "content": prompt}], cfg)
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return 0
    if not isinstance(data, dict):
        return 0
    # 兼容旧输出格式(顶层直接是 名字→事实列表)
    facts_map = data.get("facts") if isinstance(data.get("facts"), dict) else \
        {k: v for k, v in data.items() if k != "updates" and isinstance(v, list)}
    updates_map = data.get("updates") if isinstance(data.get("updates"), dict) else {}
    n = 0
    for nm, facts in (facts_map or {}).items():
        wid = name2wxid.get(nm) or nm
        norm = []
        for f in (facts or []):
            if isinstance(f, str):
                norm.append({"text": f, "type": "unknown"})
            elif isinstance(f, dict) and f.get("text"):
                norm.append({"text": str(f["text"]), "type": f.get("type")})
        if norm:
            _add_facts(wid, nm, norm, scope=chat_scope or ("chat:" + str(wid)),
                       source_msg_id=src_id)
            n += 1
    # 应用更正/取消：只接受真实存在的 ID(防臆造)；带来源消息ID
    for nm, ups in (updates_map or {}).items():
        wid = name2wxid.get(nm)
        if not wid:
            continue                       # 发言人对不上→不动
        prof = load_profile(wid)
        ids = {f["id"]: f for f in prof.get("facts", [])}
        changed = False
        for u in (ups or []):
            if not isinstance(u, dict):
                continue
            fid = u.get("id")
            f = ids.get(fid)
            if not f or f.get("status") != "active":
                continue                   # ID 不存在/已失效→拒绝(保守)
            if u.get("op") == "cancel":
                f["status"] = "cancelled"
                f["source_msg_id"] = f.get("source_msg_id") or src_id
                changed = True
            elif u.get("op") == "supersede" and (u.get("new_text") or "").strip():
                changed = True
        if changed:
            save_profile(prof)
            n += 1
        # supersede 走 mark_superseded(会重载文件,故放 save 之后)
        for u in (ups or []):
            if isinstance(u, dict) and u.get("op") == "supersede" \
                    and (u.get("new_text") or "").strip() and u.get("id") in ids \
                    and ids[u["id"]].get("status") == "active":
                mark_superseded(wid, u["id"], u["new_text"].strip(),
                                u.get("type") or "unknown", source_msg_id=src_id)
    return n


@sessions.task
def compress(wxid, cfg=None):
    """事实过多→用 LLM 压成 summary + 精简 facts（保留最多 ~15 条关键事实）。
    只压 active 的事实；保留类型；被压缩掉的原文不动 event_ts/来源语义(压缩产物标记 confidence=low)。"""
    p = load_profile(wxid)
    active = [f for f in p.get("facts", []) if f.get("status") == "active"]
    facts = [f["text"] for f in active]
    if not facts or not llm.available():
        return
    prompt = ("把下面关于同一个人的事实去重、合并、提炼，输出 JSON："
              "{\"summary\":\"两三句人物画像\",\"facts\":[{\"text\":\"关键事实\",\"type\":\"..\"}](≤15条)}。"
              "只输出 JSON。\n\n" + "\n".join("- " + f for f in facts))
    try:
        raw = llm.chat("你是画像整理器，只输出 JSON。",
                       [{"role": "user", "content": prompt}], cfg)
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return
    now = int(time.time())
    p["summary"] = (data.get("summary") or "").strip()
    kept = []
    for f in data.get("facts", []):
        text = (f.get("text") if isinstance(f, dict) else str(f)).strip()
        if not text:
            continue
        ftype = (f.get("type") if isinstance(f, dict) else "") or "unknown"
        kept.append({"id": _gen_id(text + str(now)), "text": text,
                     "type": ftype if ftype in FACT_TYPES else "unknown",
                     "source_msg_id": None, "recorded_ts": now, "event_ts": None,
                     "status": "active", "scope": "chat:" + str(wxid),
                     "expires_ts": None, "confidence": "low", "superseded_by": None})
    # 非 active 的(cancelled/superseded)原样保留，供"历史计划"追溯
    p["facts"] = kept + [f for f in p.get("facts", []) if f.get("status") != "active"]
    save_profile(p)


# ---------------- 按需检索(替代全量注入) ----------------
def _rel_time(event_ts, recorded_ts, now=None):
    """给一条事实生成时间标注。优先事件时间；只有记录时间时标'记录于'并说明非事件时间。"""
    now = now or time.time()
    ts, kind = (event_ts, "") if event_ts else (recorded_ts, "记录于")
    if not ts:
        return "时间未知"
    d = now - ts
    if d < 3600:
        rel = "刚刚"
    elif d < 86400:
        rel = f"{int(d // 3600)}小时前"
    elif d < 86400 * 30:
        rel = f"{int(d // 86400)}天前"
    elif d < 86400 * 365:
        rel = f"{int(d // 86400 // 30)}个月前"
    else:
        rel = f"{int(d // 86400 // 365)}年前"
    return (kind + rel).strip()


def _tokset(text):
    from core import knowledge
    return set(knowledge._ngramize(text or "").split())


# 话题簇：解决"不吃辣"↔"晚饭推荐"这类无词面重合但同话题的召回缺口。
# 【明确的局限】这是最小启发式词典,只覆盖常见生活话题,不是语义理解;
# 词典外的同义/隐喻场景仍可能漏召回——不能声称有限规则覆盖所有语义。
_TOPIC_CLUSTERS = [
    ("饮食", "吃 饭 餐 菜 辣 甜 咸 口味 忌口 火锅 烧烤 外卖 餐厅 饭店 早餐 午饭 午餐 晚饭 晚餐 夜宵 零食 好吃 请客 聚餐 下馆子 点菜 食 喝 奶茶 咖啡 酒"),
    ("出行计划", "去 计划 约 行程 出发 出行 旅游 旅行 机票 高铁 酒店 民宿 周末 假期 安排 见面 聚 玩 逛"),
    ("工作学业", "工作 上班 加班 项目 老板 同事 开会 出差 工资 离职 跳槽 考试 学习 上课 作业 论文"),
    ("身体健康", "身体 生病 感冒 发烧 医院 医生 药 过敏 体检 睡 失眠 累 锻炼 健身 减肥"),
    ("家庭",   "家 爸 妈 儿子 女儿 孩子 老婆 老公 哥 姐 弟 妹 奶奶 爷爷 亲戚 接送 放学"),
]
_TOPIC_CLUSTERS = [(name, set(words.split())) for name, words in _TOPIC_CLUSTERS]


def _topics_of(text):
    """文本命中的话题簇下标集合(词典启发式,见上方局限说明)。"""
    t = text or ""
    return {i for i, (_, words) in enumerate(_TOPIC_CLUSTERS)
            if any(w in t for w in words)}


def select_memories(wxid, query, recent_topic="", scope=None, budget=None,
                    now=None, cfg=None):
    """按需检索：返回与当前话题相关、在作用域内、状态有效的记忆(最多 budget 条)。

    - query：当前待回复消息；recent_topic：近期话题补充(支持"那个/后来呢"接续)。
    - scope：当前会话作用域(如 "chat:<wxid>" 或 "group:<room>")。私聊记忆不在群里默认公开。
    - 允许返回空；无命中【不】拿 summary 兜底。
    - 相关性用 n-gram 重叠(近似，非语义)——只作候选，不作充分使用理由，最终由调用方(结合是否追问)决定明示/隐式。
    返回 [{id,text,type,event_ts,recorded_ts,confidence,time_label,score}]，按相关度降序。
    """
    now = now or time.time()
    budget = budget or (cfg or {}).get("memory_budget") or DEFAULT_BUDGET
    p = load_profile(wxid)
    q = _tokset(query) | _tokset(recent_topic)
    qtopics = _topics_of((query or "") + " " + (recent_topic or ""))
    cands = []
    for f in p.get("facts", []):
        if f.get("status") != "active":
            continue                     # cancelled/superseded 不主动用(可经工具追溯)
        if f.get("expires_ts") and f["expires_ts"] < now:
            continue                     # 过期(如旧情绪)
        text = f.get("text") or ""
        if not text or _JUNK_RE.search(text):
            continue                     # 无价值/系统通知
        if scope and f.get("scope") and f["scope"] != scope \
                and not str(f["scope"]).startswith("global"):
            continue                     # 作用域隔离：私聊记忆不串到群里
        # 词面 n-gram 重叠(强信号) + 话题簇重合(弱信号,补"不吃辣↔晚饭推荐"类缺口)
        overlap = len(q & _tokset(text)) if q else 0
        topic_hit = len(qtopics & _topics_of(text))
        score = overlap * 2 + topic_hit
        if score <= 0:
            continue                     # 与当前话题无关 → 不选(允许最终为空)
        cands.append((score, f))
    cands.sort(key=lambda x: -x[0])
    out = []
    for score, f in cands[:budget]:
        out.append({"id": f["id"], "text": f["text"], "type": f.get("type"),
                    "event_ts": f.get("event_ts"), "recorded_ts": f.get("recorded_ts"),
                    "confidence": f.get("confidence"),
                    "time_label": _rel_time(f.get("event_ts"), f.get("recorded_ts"), now),
                    "score": score})
    return out


def recently_mentioned(text, recent_self_texts, thresh=0.6):
    """近似判断某条记忆是否【最近已被机器人主动说过】(避免反复复述)。

    用 n-gram 重叠比例，非语义识别——这是明确的近似方法，局限：可能漏判换了说法的复述、
    或误判用词恰好相近但语义不同的情况。仅用于"要不要主动再说一遍"，不用于永久屏蔽。
    """
    ft = _tokset(text)
    if not ft:
        return False
    for s in recent_self_texts or []:
        st = _tokset(s)
        if not st:
            continue
        if len(ft & st) / max(1, len(ft)) >= thresh:
            return True
    return False


@sessions.task
def mark_superseded(wxid, old_id, new_text, new_type="unknown", source_msg_id=None):
    """用户更正/取消旧事实时调用：把旧事实标 superseded 并追加带来源的新事实。
    需要来源支持(source_msg_id)；旧事实不删除，保留历史可追溯。返回是否成功。"""
    p = load_profile(wxid)
    now = int(time.time())
    old = next((f for f in p.get("facts", []) if f.get("id") == old_id), None)
    if not old:
        return False
    new_text = (new_text or "").strip()
    new = None
    if new_text:
        new = {"id": _gen_id(new_text + str(now)), "text": new_text,
               "type": new_type if new_type in FACT_TYPES else "unknown",
               "source_msg_id": source_msg_id, "recorded_ts": now, "event_ts": None,
               "status": "active", "scope": old.get("scope"),
               "expires_ts": None, "confidence": "high", "superseded_by": None}
        old["superseded_by"] = new["id"]
        p["facts"].append(new)
    old["status"] = "superseded"
    save_profile(p)
    return True


def profile_context(wxid, limit=12):
    """(兼容旧调用/网页展示用) 取一段简短画像文本；无则空串。
    注意：这是【全量】视图，仅供人看或调试，回复注入请改用 select_memories()。"""
    p = load_profile(wxid)
    parts = []
    if p.get("summary"):
        parts.append(p["summary"])
    facts = [f["text"] for f in p.get("facts", []) if f.get("status") == "active"][-limit:]
    parts += facts
    if not parts:
        return ""
    who = p.get("name") or wxid
    return f"【关于 {who} 的已知信息】\n" + "\n".join("· " + x for x in parts)
