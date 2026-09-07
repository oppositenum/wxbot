"""长期记忆 / 人物画像：从对话里增量抽取每个人的稳定事实，累积存档，回复时注入。

每人一份 accounts/<wxid>/profiles/<对方wxid>.json：
  {wxid, name, facts:[{fact, ts}], summary, updated}
- extract_from_messages(): 让 LLM 从最近对话里抽取"关于某人的稳定事实"，合并去重入档。
- profile_context(wxid): 取一段简短画像文本，供机器人回复时作背景注入。
- 事实过多时 compress()：用 LLM 把碎事实压成 summary + 精简 facts。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from core import llm  # noqa: E402

MAX_FACTS = 40             # 超过就触发压缩


def _dir():
    d = os.path.join(config.account_dir(), "profiles")
    os.makedirs(d, exist_ok=True)
    return d


def _path(wxid):
    safe = (wxid or "unknown").replace("/", "_").replace("@", "_at_")
    return os.path.join(_dir(), safe + ".json")


def load_profile(wxid):
    try:
        return json.load(open(_path(wxid), encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"wxid": wxid, "name": "", "facts": [], "summary": "", "updated": 0}


def save_profile(p):
    p["updated"] = int(time.time())
    json.dump(p, open(_path(p["wxid"]), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def list_profiles():
    out = []
    for fn in sorted(os.listdir(_dir())) if os.path.isdir(_dir()) else []:
        if fn.endswith(".json"):
            try:
                p = json.load(open(os.path.join(_dir(), fn), encoding="utf-8"))
                out.append({"wxid": p.get("wxid"), "name": p.get("name"),
                            "facts": len(p.get("facts", [])),
                            "summary": p.get("summary", ""),
                            "updated": p.get("updated", 0)})
            except Exception:  # noqa: BLE001
                pass
    return out


def _add_facts(wxid, name, facts):
    if not facts:
        return
    p = load_profile(wxid)
    if name and not p.get("name"):
        p["name"] = name
    have = {f["fact"] for f in p.get("facts", [])}
    now = int(time.time())
    for f in facts:
        f = (f or "").strip()
        if f and f not in have:
            p.setdefault("facts", []).append({"fact": f, "ts": now})
            have.add(f)
    save_profile(p)
    if len(p.get("facts", [])) > MAX_FACTS:
        compress(wxid)


def extract_from_messages(msgs, me=None, cfg=None):
    """从一批消息里抽取"关于各发言人的稳定事实"，合并进各自画像。返回更新的人数。

    只抽稳定、有长期价值的信息(身份/职业/偏好/关系/长期项目)，忽略寒暄和一次性内容。
    """
    if not llm.available():
        return 0
    # 组织成"名字: 内容"的转录，收集 名字→wxid 映射
    name2wxid = {}
    lines = []
    for m in msgs:
        if m.get("is_self"):
            continue
        wid = m.get("sender")
        nm = m.get("sender_name") or wid or "对方"
        if wid:
            name2wxid[nm] = wid
        c = (m.get("content") or "").strip()
        if c and not c.startswith("["):
            lines.append(f"{nm}: {c}")
    if len(lines) < 3:
        return 0
    transcript = "\n".join(lines[-60:])
    prompt = (
        "下面是一段微信聊天记录。请为其中每个【发言人】抽取关于TA的稳定、有长期价值的事实"
        "(如身份/职业/居住地/家庭/长期偏好/在做的项目/与我的关系)，忽略寒暄、情绪和一次性内容。"
        "严格输出 JSON：{\"发言人名字\": [\"事实1\",\"事实2\"]}；没有可抽取的就输出 {}。不要多余文字。\n\n"
        + transcript)
    try:
        raw = llm.chat("你是信息抽取器，只输出 JSON。",
                       [{"role": "user", "content": prompt}], cfg)
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for nm, facts in (data or {}).items():
        wid = name2wxid.get(nm) or nm
        if isinstance(facts, list) and facts:
            _add_facts(wid, nm, [str(f) for f in facts])
            n += 1
    return n


def compress(wxid, cfg=None):
    """事实过多→用 LLM 压成 summary + 精简 facts（保留最多 ~15 条关键事实）。"""
    p = load_profile(wxid)
    facts = [f["fact"] for f in p.get("facts", [])]
    if not facts or not llm.available():
        return
    prompt = ("把下面关于同一个人的事实去重、合并、提炼，输出 JSON："
              "{\"summary\":\"两三句人物画像\",\"facts\":[\"关键事实(≤15条)\"]}。只输出 JSON。\n\n"
              + "\n".join("- " + f for f in facts))
    try:
        raw = llm.chat("你是画像整理器，只输出 JSON。",
                       [{"role": "user", "content": prompt}], cfg)
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return
    now = int(time.time())
    p["summary"] = (data.get("summary") or "").strip()
    p["facts"] = [{"fact": str(f).strip(), "ts": now}
                  for f in data.get("facts", []) if str(f).strip()]
    save_profile(p)


def profile_context(wxid, limit=12):
    """取一段简短画像文本，供回复时注入；无则返回空串。"""
    p = load_profile(wxid)
    parts = []
    if p.get("summary"):
        parts.append(p["summary"])
    facts = [f["fact"] for f in p.get("facts", [])][-limit:]
    parts += facts
    if not parts:
        return ""
    who = p.get("name") or wxid
    return f"【关于 {who} 的已知信息】\n" + "\n".join("· " + x for x in parts)
