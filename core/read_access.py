"""Server-issued read scope for model tools. Paths stay bound to the issuing account."""
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import config


@dataclass(frozen=True)
class Access:
    account: str
    root: str
    chat: str
    members: frozenset
    names: tuple


def issue(chat):
    """Called with the server's conversation, never tool input or a client-supplied scope."""
    from core import contacts
    account = config.account_key()
    if not account or account == "default" or not isinstance(chat, str) or not chat.strip():
        return None
    root = os.path.realpath(config.account_dir())
    try:
        if chat.endswith("@chatroom"):
            if chat not in {g["username"] for g in contacts.list_groups()}:
                return None
            rows = contacts.group_members(chat)
            names = tuple((m["wxid"], m.get("name") or m["wxid"]) for m in rows)
        else:
            rows = [c for c in contacts.list_contacts() if c.get("username") == chat]
            if len(rows) != 1:
                return None
            c = rows[0]
            names = tuple((chat, n) for n in {chat, c.get("name"), c.get("nick_name"), c.get("remark")} if n)
        access = Access(account, root, chat, frozenset(w for w, _ in names), names)
        return access if valid(access) else None
    except Exception:
        return None


def valid(access, chat=None):
    return (isinstance(access, Access) and bool(access.account) and access.account != "default"
            and bool(access.chat) and bool(access.members)
            and access.account == config.account_key()
            and access.root == os.path.realpath(config.account_dir())
            and (chat is None or chat == access.chat))


def _json(access, relative, default):
    # Only constant filenames or server-resolved member IDs may reach this function.
    path = Path(access.root, relative).resolve()
    if os.path.commonpath([access.root, str(path)]) != access.root:
        raise PermissionError("invalid account path")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def profile(access, member):
    from core import memory
    if not valid(access) or member not in access.members:
        raise PermissionError("read scope denied")
    safe = member.replace("/", "_").replace("@", "_at_")
    p = _json(access, "profiles/" + safe + ".json", {})
    if p.get("wxid", member) != member:
        raise PermissionError("profile identity mismatch")
    return {"wxid": member, "facts": [memory._migrate_fact(f, member) for f in p.get("facts", [])]}


def _connect(access, relative):
    path = Path(access.root, relative).resolve()
    if os.path.commonpath([access.root, str(path)]) != access.root:
        raise PermissionError("invalid account path")
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def history(access, limit=400):
    from core import messages, schedule
    if not valid(access):
        raise PermissionError("read scope denied")
    table = "Msg_" + hashlib.md5(access.chat.encode()).hexdigest()
    con = _connect(access, "decrypted/message.db")
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return []
        ids = dict(con.execute("SELECT rowid,user_name FROM Name2Id"))
        rows = con.execute("SELECT * FROM " + table + " ORDER BY create_time DESC LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    fires = _json(access, "schedule_fires.json", [])
    out = []
    for row in reversed(rows):
        r = dict(row)
        # Historical media is unread here; never expose binary/XML placeholders as descriptions.
        if (r.get("local_type", 0) & 0xffff) != 1:
            continue
        text = messages._decompress(r.get("message_content"), r.get("WCDB_CT_message_content"))
        who = ids.get(r.get("real_sender_id"))
        if access.chat.endswith("@chatroom"):
            group_sender, text = messages._split_group_sender(text)
            who = group_sender or who
        is_self = who == access.account
        ts = r.get("create_time") or 0
        if is_self and (text.startswith(schedule.PREFIX) or any(
                f.get("chat") == access.chat and f.get("text") == text
                and abs(ts - f.get("ts", 0)) <= schedule._FIRE_MATCH_WINDOW for f in fires)):
            continue
        out.append({"content": text, "is_self": is_self, "create_time": ts})
    return out


def kb(access, query, k):
    from core import knowledge
    if not valid(access):
        raise PermissionError("read scope denied")
    if not Path(access.root, "kb.db").exists():
        return []
    con = _connect(access, "kb.db")
    try:
        if knowledge._has_fts(con):
            toks = knowledge._ngramize(query).split()
            if not toks:
                return []
            match = " OR ".join('"' + t + '"' for t in dict.fromkeys(toks))
            rows = con.execute("SELECT title,content,source FROM docs WHERE docs MATCH ? ORDER BY bm25(docs) LIMIT ?", (match, k))
        else:
            rows = con.execute("SELECT title,content,source FROM docs WHERE content LIKE ? LIMIT ?", ("%" + query + "%", k))
        return [dict(r) for r in rows]
    finally:
        con.close()
