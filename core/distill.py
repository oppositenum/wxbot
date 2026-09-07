"""蒸馏：从群里提取某人语料 → LLM 产出「回复风格人设」→ 存 personas/<slug>.json。

用法：
  python3 -m core.distill candidates <群username>     # 列出该群发言候选人
  python3 -m core.distill run <群username> <wxid> [名字]  # 蒸馏该人
"""
import hashlib
import json
import os
import re
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from core import db, messages, contacts, llm  # noqa: E402


ANALYZER_SYSTEM = """你是聊天风格分析师。下面是某人在一个微信群里的历史发言。
请提炼出这个人的「说话风格」，产出一段【可直接用作角色扮演 system prompt】的人设描述，覆盖：
- 句式长度与标点习惯（长句/短句、省略号/感叹号/不加标点等）
- emoji/表情使用频率与偏好
- 口头禅、高频用语、中英文混用程度
- 语气与态度（吐槽/热情/高冷/自嘲…）、怼人和夸人的方式
- 回复节奏（爱连发短句 / 一次说完）
输出要求：直接写“你是<名字>，你说话……”这样的第二人称人设,150-300字,不要分析套话,要具体到能被模仿。"""


# 升级版：双线(性格/风格 + 能力/知识)结构化多层人设
STRUCT_SYSTEM = """你是人物画像分析师。下面是某人的大量微信历史发言。请做【双线分析】并输出结构化人设：
一、性格/风格线；二、能力/知识线。严格输出 JSON（不要多余文字、不要markdown代码块）：
{
  "identity": "身份定位：TA是个怎样的人、在群里/关系里的角色(一两句)",
  "values": "价值观/在意什么/表达出的立场与态度倾向",
  "style": "说话风格：句长、标点、emoji、口头禅、中英混用、语气、连发还是一次说完(要具体可模仿)",
  "knowledge": "能力/知识领域：擅长什么、常聊的专业话题、认知水平",
  "quirks": "癖好/独特习惯：独特用词、梗、固定回应模式、边界(什么不会说)",
  "catchphrases": ["高频口头禅/固定表达，最多8个"]
}
每个字段要具体、来自证据、能指导模仿；没有素材的字段给空串或空数组。"""


def _analyze_structured(name, stats, corpus, corrections=None):
    """双线结构化分析，返回 layers dict。失败时退回把单段人设塞进 style。"""
    extra = ""
    if corrections:
        extra = "\n\n【用户已给的纠正，务必遵守】\n" + "\n".join("- " + c for c in corrections)
    cfg = dict(llm.load_cfg())
    cfg["max_tokens"] = max(1800, cfg.get("max_tokens", 0))   # 结构化JSON较长，别被截断
    user = (f"名字：{name}\n统计：{json.dumps(stats, ensure_ascii=False)}\n"
            f"历史发言（每行一条）：\n{corpus}{extra}")
    for _ in range(2):                    # 模型偶尔不吐纯 JSON，重试一次
        try:
            raw = llm.chat(STRUCT_SYSTEM, [{"role": "user", "content": user}], cfg)
            raw = raw.replace("```json", "").replace("```", "")
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
            d = json.loads(raw)
            return {k: d.get(k, "" if k != "catchphrases" else [])
                    for k in ("identity", "values", "style", "knowledge",
                              "quirks", "catchphrases")}
        except Exception:  # noqa: BLE001
            continue
    return None


def _render_persona(name, layers, corrections=None):
    """把结构化多层渲染成第二人称、可直接当 system prompt 的人设文本。"""
    L = layers or {}
    parts = [f"你是{name}。请始终以第一人称、用下述人设的口吻聊天。"]
    if L.get("identity"):
        parts.append(f"【身份】{L['identity']}")
    if L.get("values"):
        parts.append(f"【价值观】{L['values']}")
    if L.get("style"):
        parts.append(f"【说话风格】{L['style']}")
    if L.get("catchphrases"):
        parts.append("【口头禅】" + "、".join(L["catchphrases"][:8]))
    if L.get("knowledge"):
        parts.append(f"【擅长/常聊】{L['knowledge']}")
    if L.get("quirks"):
        parts.append(f"【癖好/边界】{L['quirks']}")
    if corrections:
        parts.append("【特别注意(用户纠正)】" + "；".join(corrections))
    return "\n".join(parts)


def _sample_pool(texts, n=150):
    """从语料里挑代表性样例池(供检索式 few-shot)：去重、去噪、时间均匀采样。"""
    seen, cleaned = set(), []
    for t in texts:
        t = (t or "").strip()
        if len(t) < 2 or t in seen:      # 去重、丢过短
            continue
        seen.add(t)
        cleaned.append(t)
    if len(cleaned) <= n:
        return cleaned
    step = len(cleaned) / float(n)       # 时间均匀采样(texts 已按时间序)
    return [cleaned[int(i * step)] for i in range(n)]


def _build_persona(name, wxid, group, texts, stats, corrections=None):
    """核心：双线结构化分析→多层人设+渲染文本+代表样例库。LLM 不可用则退回单段。"""
    # 分析用样本：时间均匀采样，最多 ~320 条控制 token
    if len(texts) > 320:
        step = len(texts) / 320.0
        asample = [texts[int(i * step)] for i in range(320)]
    else:
        asample = texts
    corpus = "\n".join(asample)
    layers = _analyze_structured(name, stats, corpus, corrections)
    if layers:
        persona = _render_persona(name, layers, corrections)
    else:                                # 退回旧的单段人设
        layers = {}
        persona = llm.chat(ANALYZER_SYSTEM, [{"role": "user", "content":
            f"名字：{name}\n统计：{json.dumps(stats, ensure_ascii=False)}\n"
            f"历史发言（每行一条）：\n{corpus}"}])
    return {"slug": re.sub(r"[^0-9a-zA-Z一-鿿]+", "-", name).strip("-") or (wxid or "persona"),
            "name": name, "wxid": wxid, "group": group, "stats": stats,
            "persona": persona, "layers": layers,
            "samples": _sample_pool(texts, 150),
            "corrections": corrections or []}


def _save_persona(data):
    os.makedirs(config.personas_dir(), exist_ok=True)
    with open(os.path.join(config.personas_dir(), data["slug"] + ".json"),
              "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def pick_samples(persona, query, k=10):
    """检索式 few-shot：从样例库里挑与当前话题最像的历史原话(n-gram 重叠打分)。"""
    from core import knowledge
    samples = persona.get("samples") or []
    if not samples:
        return []
    toks = knowledge._ngramize(query or "").split()
    if not toks:
        return samples[-k:]
    scored = []
    for s in samples:
        sg = knowledge._ngramize(s)
        hit = sum(sg.count(t) for t in toks)
        if hit:
            scored.append((hit, s))
    scored.sort(key=lambda x: -x[0])
    top = [s for _, s in scored[:k]]
    return top or samples[-k:]           # 无匹配则退回最近若干条


def correct(slug, feedback):
    """纠正精修：把用户反馈并入 corrections 并重渲染人设文本(不必重跑蒸馏)。"""
    p = load_persona(slug)
    if not p:
        return None
    corr = p.get("corrections") or []
    corr.append(feedback.strip())
    p["corrections"] = corr
    if p.get("layers"):
        p["persona"] = _render_persona(p["name"], p["layers"], corr)
    else:                                # 老人设无 layers：把纠正附加到文本尾
        p["persona"] = (p.get("persona") or "") + "\n【特别注意(用户纠正)】" + "；".join(corr)
    _save_persona(p)
    return p


def _person_msgs(group_username, wxid, limit=2000):
    """取某人在群里的发言文本 + 统计。"""
    table = "Msg_" + hashlib.md5(group_username.encode()).hexdigest()
    con = db.connect("message")
    try:
        if not db.table_exists(con, table):
            return [], {}
        n2i = {r[1]: r[0] for r in con.execute(
            "SELECT rowid,user_name FROM Name2Id").fetchall()}
        rid = n2i.get(wxid)
        if rid is None:
            return [], {}
        cols = set(db.columns(con, table))
        ctc = "WCDB_CT_message_content" if "WCDB_CT_message_content" in cols else None
        sel = "local_id,local_type,create_time,message_content" + (f",{ctc}" if ctc else "")
        rows = con.execute(
            f"SELECT {sel} FROM {table} WHERE real_sender_id=? ORDER BY create_time",
            (rid,)).fetchall()
    finally:
        con.close()

    texts, hours = [], collections.Counter()
    from datetime import datetime
    for r in rows:
        d = dict(r)
        rt = (d.get("local_type") or 0) & 0xFFFF
        if rt not in (1, 49):
            continue
        body = messages._decompress(d.get("message_content"), d.get(ctc) if ctc else None)
        _, body = messages._split_group_sender(body)
        if rt == 49:
            title, _ = messages._parse_refer(body)
            body = title or ""
        body = (body or "").strip()
        if body and not body.startswith("<"):
            texts.append(body)
            if d.get("create_time"):
                hours[datetime.fromtimestamp(d["create_time"]).hour] += 1
    words = collections.Counter()
    for t in texts:
        for w in re.findall(r"[一-鿿]{2,4}|[a-zA-Z]{2,}", t):
            words[w] += 1
    stats = {"count": len(texts),
             "avg_len": round(sum(len(t) for t in texts) / max(1, len(texts)), 1),
             "top_words": [w for w, _ in words.most_common(15)],
             "active_hours": [h for h, _ in hours.most_common(3)]}
    return texts, stats


def _self_msgs(limit=8000):
    """汇总"自己"在所有群聊+私聊里的发言(message 库 + 公众号 biz 库)。"""
    me = config.wxid()
    if not me:
        return [], {}
    from datetime import datetime
    items = []                    # (create_time, text)
    for dbkey in ("message", "biz_message"):
        try:
            con = db.connect(dbkey)
        except Exception:  # noqa: BLE001
            continue
        try:
            n2i = {r[0]: r[1] for r in con.execute(
                "SELECT rowid,user_name FROM Name2Id").fetchall()}
            self_rids = [rid for rid, u in n2i.items() if u == me]
            if not self_rids:
                continue
            placeholders = ",".join("?" * len(self_rids))
            tbls = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'Msg\\_%' ESCAPE '\\'").fetchall()]
            for t in tbls:
                cols = set(db.columns(con, t))
                if "real_sender_id" not in cols:
                    continue
                ctc = "WCDB_CT_message_content" if "WCDB_CT_message_content" in cols else None
                sel = "local_type,create_time,message_content" + (f",{ctc}" if ctc else "")
                try:
                    rows = con.execute(
                        f"SELECT {sel} FROM {t} WHERE real_sender_id IN ({placeholders})",
                        self_rids).fetchall()
                except Exception:  # noqa: BLE001
                    continue
                for r in rows:
                    d = dict(r)
                    rt = (d.get("local_type") or 0) & 0xFFFF
                    if rt not in (1, 49):
                        continue
                    body = messages._decompress(
                        d.get("message_content"), d.get(ctc) if ctc else None)
                    _, body = messages._split_group_sender(body)
                    if rt == 49:
                        title, _ = messages._parse_refer(body)
                        body = title or ""
                    body = (body or "").strip()
                    if body and not body.startswith("<"):
                        items.append((d.get("create_time") or 0, body))
        finally:
            con.close()

    items.sort(key=lambda x: x[0])
    texts = [t for _, t in items]
    hours = collections.Counter()
    for ts, _ in items:
        if ts:
            hours[datetime.fromtimestamp(ts).hour] += 1
    words = collections.Counter()
    for t in texts:
        for w in re.findall(r"[一-鿿]{2,4}|[a-zA-Z]{2,}", t):
            words[w] += 1
    stats = {"count": len(texts),
             "avg_len": round(sum(len(t) for t in texts) / max(1, len(texts)), 1),
             "top_words": [w for w, _ in words.most_common(15)],
             "active_hours": [h for h, _ in hours.most_common(3)]}
    return texts[-limit:], stats


def run_self(name=None):
    """蒸馏"自己"：汇总所有会话里自己发的话，产出多层结构化人设。"""
    texts, stats = _self_msgs()
    if stats.get("count", 0) < 20:
        return None, f"你自己的发言太少({stats.get('count',0)}条),不足以蒸馏"
    me = config.wxid()
    name = name or "我"
    data = _build_persona(name, me, "(自己·全部会话)", texts, stats)
    if not data["slug"] or data["slug"] == me:
        data["slug"] = "self"
    _save_persona(data)
    return data, "ok"


def candidates(group_username):
    """列出群里发言最多的人。"""
    table = "Msg_" + hashlib.md5(group_username.encode()).hexdigest()
    con = db.connect("message")
    try:
        if not db.table_exists(con, table):
            return []
        n2i = {r[0]: r[1] for r in con.execute(
            "SELECT rowid,user_name FROM Name2Id").fetchall()}
        cnt = collections.Counter()
        for r in con.execute(f"SELECT real_sender_id FROM {table}").fetchall():
            u = n2i.get(r[0])
            if u:
                cnt[u] += 1
    finally:
        con.close()
    cmap = {c["username"]: c for c in contacts.list_contacts()}
    members = {m["wxid"]: m for m in contacts.group_members(group_username)}
    out = []
    for wxid, c in cnt.most_common(20):
        name = ((cmap.get(wxid) or {}).get("name")
                or (members.get(wxid) or {}).get("name") or wxid)
        out.append({"wxid": wxid, "name": name, "count": c})
    return out


def run(group_username, wxid, name=None):
    texts, stats = _person_msgs(group_username, wxid)
    if stats.get("count", 0) < 20:
        return None, f"该人发言太少({stats.get('count',0)}条),不足以蒸馏"
    members = {m["wxid"]: m for m in contacts.group_members(group_username)}
    name = name or (members.get(wxid) or {}).get("name") or wxid
    data = _build_persona(name, wxid, group_username, texts, stats)
    _save_persona(data)
    return data, "ok"


def load_persona(slug):
    p = os.path.join(config.personas_dir(), slug + ".json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    return None


def list_personas():
    if not os.path.isdir(config.personas_dir()):
        return []
    out = []
    for fn in os.listdir(config.personas_dir()):
        if fn.endswith(".json"):
            try:
                d = json.load(open(os.path.join(config.personas_dir(), fn), encoding="utf-8"))
                out.append({"slug": d["slug"], "name": d["name"],
                            "count": d.get("stats", {}).get("count")})
            except Exception:  # noqa: BLE001
                pass
    return out


def import_persona(name, persona, samples=None, slug=None):
    """导入自定义人设（用户手写/从别处拿来）。"""
    slug = slug or re.sub(r"[^0-9a-zA-Z一-鿿]+", "-", name).strip("-") or "persona"
    os.makedirs(config.personas_dir(), exist_ok=True)
    data = {"slug": slug, "name": name, "wxid": "", "group": "imported",
            "stats": {"count": len(samples or [])}, "persona": persona,
            "samples": (samples or [])[:40]}
    with open(os.path.join(config.personas_dir(), slug + ".json"), "w",
              encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def delete_persona(slug):
    p = os.path.join(config.personas_dir(), slug + ".json")
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "candidates":
        for c in candidates(sys.argv[2]):
            print(f"  {c['count']:5}条  {c['wxid']:24} {c['name']}")
    elif cmd == "run":
        wxid = sys.argv[3]
        name = sys.argv[4] if len(sys.argv) > 4 else None
        data, msg = run(sys.argv[2], wxid, name)
        if data:
            print("✅ 人设已生成:", data["slug"])
            print(data["persona"])
        else:
            print("✗", msg)


if __name__ == "__main__":
    main()
