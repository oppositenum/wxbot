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
    """蒸馏"自己"：汇总所有会话里自己发的话，产出自己的说话风格人设。"""
    texts, stats = _self_msgs()
    if stats.get("count", 0) < 20:
        return None, f"你自己的发言太少({stats.get('count',0)}条),不足以蒸馏"
    me = config.wxid()
    name = name or "我"
    # 均匀采样(跨全部会话，最多 ~400 条控制 token)
    if len(texts) > 400:
        step = len(texts) / 400.0
        sample = [texts[int(i * step)] for i in range(400)]
    else:
        sample = texts
    corpus = "\n".join(sample)
    persona = llm.chat(
        ANALYZER_SYSTEM,
        [{"role": "user", "content":
          f"名字：{name}(这是我本人在各个群聊/私聊里的历史发言)\n"
          f"统计：{json.dumps(stats, ensure_ascii=False)}\n"
          f"历史发言（每行一条）：\n{corpus}"}])
    slug = re.sub(r"[^0-9a-zA-Z一-鿿]+", "-", name).strip("-") or "self"
    os.makedirs(config.personas_dir(), exist_ok=True)
    data = {"slug": slug, "name": name, "wxid": me, "group": "(自己·全部会话)",
            "stats": stats, "persona": persona, "samples": sample[-40:]}
    with open(os.path.join(config.personas_dir(), slug + ".json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
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
    # 取样本(均匀采样,最多 400 条,控制 token)
    sample = texts[-400:] if len(texts) > 400 else texts
    corpus = "\n".join(sample[-300:])
    persona = llm.chat(ANALYZER_SYSTEM,
                       [{"role": "user", "content":
                         f"名字：{name}\n统计：{json.dumps(stats, ensure_ascii=False)}\n"
                         f"历史发言（每行一条）：\n{corpus}"}])
    slug = re.sub(r"[^0-9a-zA-Z一-鿿]+", "-", name).strip("-") or wxid
    os.makedirs(config.personas_dir(), exist_ok=True)
    data = {"slug": slug, "name": name, "wxid": wxid, "group": group_username,
            "stats": stats, "persona": persona, "samples": sample[-40:]}
    with open(os.path.join(config.personas_dir(), slug + ".json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
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
