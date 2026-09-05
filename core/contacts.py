"""联系人 / 群 / 群成员 读取。"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from core import db, protobuf  # noqa: E402

WXID_RE = re.compile(rb"(wxid_[0-9a-zA-Z]+|[0-9a-zA-Z_-]+@(?:chatroom|openim))")


def _contact_rows():
    con = db.connect("contact")
    try:
        cols = set(db.columns(con, "contact"))
        want = ["username", "alias", "nick_name", "remark", "local_type",
                "small_head_url", "big_head_url", "description"]
        sel = [c for c in want if c in cols]
        if "delete_flag" in cols:
            where = "WHERE COALESCE(delete_flag,0)=0"
        else:
            where = ""
        rows = [dict(r) for r in con.execute(
            f"SELECT {','.join(sel)} FROM contact {where}").fetchall()]
        return rows
    finally:
        con.close()


def display_name(row):
    return (row.get("remark") or row.get("nick_name")
            or row.get("alias") or row.get("username") or "")


def list_contacts():
    """私聊联系人：排除群、公众号、企业微信等。"""
    out = []
    for r in _contact_rows():
        u = r.get("username") or ""
        if u.endswith("@chatroom") or u.startswith("gh_") or u.endswith("@openim"):
            continue
        if u in ("filehelper", "weixin", "fmessage", "medianote") or u.startswith("@"):
            # filehelper 保留（文件传输助手），其余系统账号跳过
            if u != "filehelper":
                continue
        out.append({
            "username": u,
            "name": display_name(r),
            "nick_name": r.get("nick_name"),
            "remark": r.get("remark"),
            "alias": r.get("alias"),
            "head_url": r.get("small_head_url") or r.get("big_head_url"),
        })
    out.sort(key=lambda x: x["name"])
    return out


def _contact_map():
    m = {}
    for r in _contact_rows():
        m[r.get("username")] = r
    return m


def all_names():
    """username -> 显示名（含公众号/服务号等，用于名字解析）。"""
    m = {}
    for r in _contact_rows():
        u = r.get("username")
        if u:
            m[u] = {"name": display_name(r),
                    "head_url": r.get("small_head_url") or r.get("big_head_url")}
    return m


def list_groups():
    """群列表：来自 chat_room 表，join contact 取群名。"""
    con = db.connect("contact")
    try:
        rooms = []
        if db.table_exists(con, "chat_room"):
            cols = set(db.columns(con, "chat_room"))
            sel = [c for c in ["username", "owner", "ext_buffer"] if c in cols]
            rooms = [dict(r) for r in con.execute(
                f"SELECT {','.join(sel)} FROM chat_room").fetchall()]
    finally:
        con.close()

    cmap = _contact_map()
    out = []
    for r in rooms:
        u = r.get("username") or ""
        if not u.endswith("@chatroom"):
            continue
        c = cmap.get(u, {})
        members = parse_members(r.get("ext_buffer"))
        out.append({
            "username": u,
            "name": display_name(c) if c else u,
            "owner": r.get("owner"),
            "member_count": len(members),
            "head_url": (c.get("small_head_url") or c.get("big_head_url")) if c else None,
        })
    out.sort(key=lambda x: x["name"])
    return out


def parse_members(ext_buffer):
    """解析 chat_room.ext_buffer(protobuf) -> [{'wxid':..,'group_nick':..}]。

    典型结构：field 1 repeated 成员子消息 { 1: wxid(str), 2: 群昵称(str) }。
    做容错：优先按该结构，取不到则回退正则扫 wxid。
    """
    if not ext_buffer:
        return []
    if isinstance(ext_buffer, str):
        ext_buffer = ext_buffer.encode("latin1", "ignore")

    members = []
    seen = set()
    try:
        top = protobuf.decode(ext_buffer)
        for entry in top.get(1, []):
            if entry["type"] != "bytes":
                continue
            sub = protobuf.as_submsg(entry["value"])
            if not sub:
                continue
            wxid = None
            nick = None
            for f1 in sub.get(1, []):
                if f1["type"] == "bytes":
                    wxid = protobuf.as_str(f1["value"])
                    break
            for f2 in sub.get(2, []):
                if f2["type"] == "bytes":
                    nick = protobuf.as_str(f2["value"])
                    break
            if wxid and (wxid.startswith("wxid_") or wxid.endswith("@openim")
                         or re.match(r"^[0-9a-zA-Z_-]+$", wxid)):
                if wxid not in seen:
                    seen.add(wxid)
                    members.append({"wxid": wxid, "group_nick": nick})
    except Exception:  # noqa: BLE001
        pass

    if not members:  # 回退：正则扫
        for m in WXID_RE.finditer(ext_buffer):
            w = m.group(1).decode("utf-8", "ignore")
            if w.endswith("@chatroom"):
                continue
            if w not in seen:
                seen.add(w)
                members.append({"wxid": w, "group_nick": None})
    return members


def group_members(chatroom_username):
    """返回群成员，附带 contact 表里的昵称/备注。"""
    con = db.connect("contact")
    try:
        row = con.execute(
            "SELECT ext_buffer FROM chat_room WHERE username=?",
            (chatroom_username,)).fetchone()
    finally:
        con.close()
    if not row:
        return []
    members = parse_members(row["ext_buffer"])
    cmap = _contact_map()
    for mem in members:
        c = cmap.get(mem["wxid"])
        mem["nick_name"] = c.get("nick_name") if c else None
        mem["remark"] = c.get("remark") if c else None
        mem["name"] = (mem.get("group_nick") or (display_name(c) if c else None)
                       or mem["wxid"])
        mem["head_url"] = (c.get("small_head_url") or c.get("big_head_url")) if c else None
    return members


if __name__ == "__main__":
    import json
    cs = list_contacts()
    gs = list_groups()
    print(f"联系人 {len(cs)} 个，示例：")
    print(json.dumps(cs[:5], ensure_ascii=False, indent=2))
    print(f"\n群 {len(gs)} 个，示例：")
    print(json.dumps(gs[:5], ensure_ascii=False, indent=2))
    if gs:
        g = gs[0]
        ms = group_members(g["username"])
        print(f"\n群「{g['name']}」成员 {len(ms)}，示例：")
        print(json.dumps(ms[:5], ensure_ascii=False, indent=2))
