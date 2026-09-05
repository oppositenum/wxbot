"""消息与会话读取，含 zstd 解压、群前缀、类型映射、引用解析。"""
import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

import zstandard

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from core import db  # noqa: E402

_ncache = {"t": 0, "m": {}}

# 撤回原文恢复：靠"两次轮询间快照对比"——某条真实消息本来在，下一轮消失了
# 且同时出现撤回系统消息 → 那条消失的就是被撤回的原文。比"按时间就近猜"可靠得多，
# 不会把"滚出 limit 窗口的老消息"误判成撤回。
# _LASTSEEN: chat -> {sid: {content, sender_name, time}}  上轮见过的真实消息(持久化)
# _REVOKED : chat -> {revoke_sid: original_content}        已定案的撤回原文(稳定，持久化)
_LASTSEEN = {}
_REVOKED = {}
_CACHE_MAX = 600
_REVOKE_NAME_RE = re.compile(r'["“](.+?)["”]\s*撤回')

_cache_loaded = False
_cache_saved_at = 0


def _cache_file():
    return os.path.join(config.account_dir(), "msgcache.json")


def _load_cache():
    global _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        d = json.load(open(_cache_file(), encoding="utf-8"))
        for ch, msgs in d.get("seen", {}).items():
            _LASTSEEN[ch] = {int(s): v for s, v in msgs.items()}
        for ch, rv in d.get("revoked", {}).items():
            _REVOKED[ch] = {int(s): v for s, v in rv.items()}
    except Exception:  # noqa: BLE001
        pass


def _save_cache(force=False):
    global _cache_saved_at
    now = time.time()
    if not force and now - _cache_saved_at < 12:
        return
    _cache_saved_at = now
    try:
        os.makedirs(config.account_dir(), exist_ok=True)
        json.dump({"seen": _LASTSEEN, "revoked": _REVOKED},
                  open(_cache_file(), "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _resolve_revokes(chat, out):
    """给本批撤回消息补上原文。就地修改 out。

    关键：微信撤回是"就地改写同一行"——server_id 不变，local_type 1→10000，
    内容被换成撤回系统消息。所以只要之前快照过这条(按 server_id)，就有原文。
    """
    _load_cache()
    snap = _LASTSEEN.setdefault(chat, {})
    revoked = _REVOKED.setdefault(chat, {})
    changed = False
    for m in out:
        sid = m.get("server_id")
        if not sid:
            continue
        if m.get("revoke"):
            orig = revoked.get(sid)
            if not orig:
                prev = snap.get(sid)          # 同一行撤回前的快照里留着原文
                if prev and prev.get("content"):
                    orig = prev["content"]
                    revoked[sid] = orig
                    changed = True
            if orig:
                m["content"] = m["content"] + "：" + orig
                m["revoke_original"] = orig
        else:                                  # 真实消息：记进快照，供之后被撤回时取原文
            con = m.get("content")
            if con and not str(con).startswith("["):
                snap[sid] = {"content": con, "sender_name": m.get("sender_name"),
                             "time": m.get("create_time") or 0}
                changed = True

    if len(snap) > _CACHE_MAX:
        for sid in sorted(snap, key=lambda s: snap[s]["time"])[:len(snap) - _CACHE_MAX]:
            del snap[sid]
    if len(revoked) > _CACHE_MAX:
        for sid in list(revoked)[:len(revoked) - _CACHE_MAX]:
            del revoked[sid]
    if changed:
        _save_cache()


def _contact_names():
    """联系人 wxid->显示名（缓存 30s）。"""
    now = time.time()
    if now - _ncache["t"] < 30 and _ncache["m"]:
        return _ncache["m"]
    m = {}
    try:
        from core import contacts
        for u, info in contacts.all_names().items():
            m[u] = info["name"]
    except Exception:  # noqa: BLE001
        pass
    _ncache["t"] = now
    _ncache["m"] = m
    return m

_ZDCTX = zstandard.ZstdDecompressor()

TYPE_NAMES = {
    1: "文本", 3: "图片", 34: "语音", 42: "名片", 43: "视频",
    47: "表情", 48: "位置", 49: "链接/文件/引用", 10000: "系统", 10002: "撤回",
}


def msg_table(username):
    return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()


def _decompress(val, ct_flag):
    """按需 zstd 解压，返回 str。"""
    if val is None:
        return ""
    if ct_flag == 4:
        raw = val if isinstance(val, (bytes, bytearray)) else str(val).encode("latin1")
        try:
            return _ZDCTX.decompress(raw).decode("utf-8", "ignore")
        except zstandard.ZstdError:
            # 有时不带完整帧，用流式
            try:
                return _ZDCTX.decompressobj().decompress(raw).decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                return ""
    if isinstance(val, (bytes, bytearray)):
        return val.decode("utf-8", "ignore")
    return str(val)


def _name2id_map(con):
    """rowid -> user_name。"""
    m = {}
    if db.table_exists(con, "Name2Id"):
        for r in con.execute("SELECT rowid, user_name FROM Name2Id").fetchall():
            m[r[0]] = r[1]
    return m


GROUP_PREFIX_RE = re.compile(r"^(wxid_[0-9a-zA-Z]+|[0-9a-zA-Z_@.-]+):\n", re.S)


def _split_group_sender(content):
    """群消息 message_content 形如 'wxid:\\n正文'，拆出 (sender_wxid, body)。"""
    m = GROUP_PREFIX_RE.match(content or "")
    if m:
        return m.group(1), content[m.end():]
    return None, content


def _parse_refer(content):
    """type=49 引用消息，解析 refermsg。返回 (title, refer_dict|None)。"""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None, None
    appmsg = root.find(".//appmsg")
    if appmsg is None:
        return None, None
    title = appmsg.findtext("title")
    refer = appmsg.find(".//refermsg")
    refer_d = None
    if refer is not None:
        rtype = refer.findtext("type")
        rc = refer.findtext("content") or ""
        _, rc = _split_group_sender(rc)            # 去掉 wxid:\n 前缀
        rc = _media_placeholder(rtype, rc)
        refer_d = {
            "displayname": refer.findtext("displayname"),
            "content": rc,
            "chatusr": refer.findtext("chatusr"),   # 被引用消息的发送者 wxid
        }
    return title, refer_d


def _parse_appmsg(content):
    """type=49 里的公众号/链接/卡片：取 title/des/url，或图文推送的多篇文章。
    返回 dict 或 None(引用消息除外，那个走 _parse_refer)。"""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None
    appmsg = root.find(".//appmsg")
    if appmsg is None or appmsg.find(".//refermsg") is not None:
        return None
    title = (appmsg.findtext("title") or "").strip()
    des = (appmsg.findtext("des") or "").strip()
    url = (appmsg.findtext("url") or "").strip()
    articles = []
    for it in appmsg.findall(".//mmreader/category/item"):
        t = (it.findtext("title") or "").strip()
        u = (it.findtext("url") or "").strip()
        if t:
            articles.append({"title": t, "url": u})
    if not (title or des or articles):
        return None
    return {"title": title, "des": des, "url": url, "articles": articles}


_REFER_TYPE = {"3": "[图片]", "34": "[语音]", "42": "[名片]", "43": "[视频]",
               "47": "[表情]", "48": "[位置]", "49": "[链接/文件]", "62": "[小视频]"}


_VOICELEN_RE = re.compile(r'voicelength="(\d+)"')
_PLAYLEN_RE = re.compile(r'playlength="(\d+)"')


def _voice_label(content):
    m = _VOICELEN_RE.search(content or "")
    if m:
        sec = max(1, round(int(m.group(1)) / 1000))
        return f'[语音 {sec}"]'
    return "[语音]"


def _video_label(content):
    m = _PLAYLEN_RE.search(content or "")
    if m and m.group(1) != "0":
        return f'[视频 {m.group(1)}"]'
    return "[视频]"


def _media_placeholder(rtype, content):
    """把媒体类的内容(常是 XML)换成占位符；纯文本原样返回。"""
    c = content or ""
    if rtype == "34":
        return _voice_label(c)
    if rtype == "43":
        return _video_label(c)
    if rtype in _REFER_TYPE:
        return _REFER_TYPE[rtype]
    if "<videomsg" in c or "cdnvideourl" in c:
        return _video_label(c)
    if "<img " in c or "cdnthumburl" in c or "aeskey" in c and "<msg" in c:
        return "[图片]"
    if "<voicemsg" in c or "voicelength" in c:
        return _voice_label(c)
    if "<emoji " in c:
        return "[表情]"
    if "<sysmsg" in c or "revokemsg" in c:
        return "[系统消息]"
    if c.lstrip().startswith("<"):          # 任何残留 XML 都别直接显示
        return "[消息]"
    return c


def _parse_sysmsg(content):
    """系统消息：撤回等，返回友好文本或 None。"""
    if not content or "sysmsg" not in content and "revokemsg" not in content:
        return None
    if "revokemsg" in content:
        m = re.search(r"<replacemsg><!\[CDATA\[(.*?)\]\]></replacemsg>", content, re.S)
        if m:
            return m.group(1).strip()
        m = re.search(r"<(?:replacemsg|content)>(.*?)</(?:replacemsg|content)>",
                      content, re.S)
        if m:
            return m.group(1).replace("<![CDATA[", "").replace("]]>", "").strip()
        return "对方撤回了一条消息"
    return None


_AT_RE = re.compile(r"<atuserlist>(.*?)</atuserlist>", re.S)


def _parse_atlist(source_xml):
    """从消息 source XML 解析 @ 的 wxid 列表。"""
    if not source_xml:
        return []
    m = _AT_RE.search(source_xml)
    if not m:
        return []
    raw = m.group(1).replace("<![CDATA[", "").replace("]]>", "")
    return [w.strip() for w in re.split(r"[,;，、\s]+", raw) if w.strip()]


def get_messages(username, limit=50, before=None):
    """返回某会话最近消息（时间正序）。普通聊天在 message 库，公众号/服务号在 biz 库。"""
    table = msg_table(username)
    con = db.connect("message")
    if not db.table_exists(con, table):        # 普通库没有→找公众号(biz)库
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
        con = None
        try:
            bcon = db.connect("biz_message")
            if db.table_exists(bcon, table):
                con = bcon
            else:
                bcon.close()
        except Exception:  # noqa: BLE001
            pass
        if con is None:
            return []
    try:
        if not db.table_exists(con, table):
            return []
        cols = set(db.columns(con, table))
        id2name = _name2id_map(con)
        is_group = username.endswith("@chatroom")
        me = config.wxid()
        names = dict(_contact_names())
        if is_group:
            try:
                from core import contacts
                for mem in contacts.group_members(username):
                    if mem.get("name"):
                        names[mem["wxid"]] = mem["name"]
            except Exception:  # noqa: BLE001
                pass

        ct_col = "WCDB_CT_message_content" if "WCDB_CT_message_content" in cols else None
        sct_col = "WCDB_CT_source" if "WCDB_CT_source" in cols else None
        sel = ["local_id", "server_id", "local_type", "real_sender_id",
               "create_time", "message_content", "source"]
        sel = [c for c in sel if c in cols]
        if ct_col:
            sel.append(ct_col)
        if sct_col:
            sel.append(sct_col)

        where = ""
        params = [limit]
        if before:
            where = "WHERE create_time < ?"
            params = [before, limit]
        rows = con.execute(
            f"SELECT {','.join(sel)} FROM {table} {where} "
            f"ORDER BY create_time DESC LIMIT ?", params).fetchall()
    finally:
        con.close()

    out = []
    for r in rows:
        d = dict(r)
        ct = d.get(ct_col) if ct_col else None
        content = _decompress(d.get("message_content"), ct)
        real_type = (d.get("local_type") or 0) & 0xFFFF

        sender_wxid = id2name.get(d.get("real_sender_id"))
        body = content
        if is_group:
            gs, body = _split_group_sender(content)
            if gs:
                sender_wxid = gs

        is_self = (sender_wxid == me) if sender_wxid else False

        # @我 检测：source 里的 atuserlist 含自己
        src = _decompress(d.get("source"), d.get(sct_col) if sct_col else None)
        at_list = _parse_atlist(src)
        at_me = bool(me) and me in at_list
        at_all = any(a.endswith("@all") for a in at_list)

        item = {
            "local_id": d.get("local_id"),
            "server_id": d.get("server_id"),
            "type": real_type,
            "type_name": TYPE_NAMES.get(real_type, str(real_type)),
            "create_time": d.get("create_time"),
            "sender": sender_wxid,
            "sender_name": names.get(sender_wxid) or sender_wxid,
            "is_self": is_self,
            "content": body,
            "at_me": at_me,
            "at_all": at_all,
            "quote_me": False,
        }
        if real_type == 49:
            title, refer = _parse_refer(body)
            if refer:
                item["content"] = title or "[引用]"
                item["refer"] = refer
                # 引用我：被引用消息的发送者是自己
                if me and refer.get("chatusr") == me:
                    item["quote_me"] = True
            else:
                art = _parse_appmsg(body)          # 公众号图文/卡片
                if art:
                    item["article"] = art
                    if art["articles"]:
                        item["content"] = "｜".join(a["title"] for a in art["articles"])
                    else:
                        item["content"] = art["title"] or art["des"] or "[链接/文件]"
                else:
                    item["content"] = title or "[链接/文件/小程序]"
        # 撤回等系统消息 → 友好文本
        sysmsg = _parse_sysmsg(body)
        if sysmsg:
            item["content"] = sysmsg
            item["type_name"] = "系统"
            item["revoke"] = True
        # 兜底：内容仍是媒体/系统 XML → 占位符（视频/名片/小视频等）
        elif item["content"] and item["content"].lstrip().startswith("<"):
            item["content"] = _media_placeholder(str(real_type), item["content"])
        out.append(item)

    out.reverse()  # 时间正序
    # 快照对比：记录本批、并给撤回消息补上原文(靠"消失检测"，可靠)
    _resolve_revokes(username, out)
    return out


def list_sessions(limit=200):
    """会话列表（session.db SessionTable），按最近排序。"""
    con = db.connect("session")
    try:
        if not db.table_exists(con, "SessionTable"):
            return []
        cols = set(db.columns(con, "SessionTable"))
        want = ["username", "type", "unread_count", "summary", "last_timestamp",
                "sort_timestamp", "last_msg_type", "last_sender_display_name",
                "is_hidden"]
        sel = [c for c in want if c in cols]
        order = "sort_timestamp" if "sort_timestamp" in cols else "last_timestamp"
        rows = [dict(r) for r in con.execute(
            f"SELECT {','.join(sel)} FROM SessionTable "
            f"ORDER BY {order} DESC LIMIT ?", (limit,)).fetchall()]
    finally:
        con.close()

    out = []
    for r in rows:
        u = r.get("username") or ""
        if r.get("is_hidden"):
            continue
        out.append({
            "username": u,
            "is_group": u.endswith("@chatroom"),
            "summary": r.get("summary"),
            "last_timestamp": r.get("last_timestamp") or r.get("sort_timestamp"),
            "unread": r.get("unread_count") or 0,
        })
    return out


if __name__ == "__main__":
    import json
    ss = list_sessions()
    print(f"会话 {len(ss)} 个，示例：")
    print(json.dumps(ss[:8], ensure_ascii=False, indent=2))
    if ss:
        target = ss[0]["username"]
        msgs = get_messages(target, limit=10)
        print(f"\n会话「{target}」最近 {len(msgs)} 条：")
        print(json.dumps(msgs, ensure_ascii=False, indent=2))
