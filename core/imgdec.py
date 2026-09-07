"""解密微信本地图片 .dat (V2 格式) 并按消息定位文件。

V2: [\\x07\\x08V2\\x08\\x07][aes_size u32][xor_size u32][pad] + AES段 + 明文段 + XOR段
AES-128-ECB(图片密钥) 解前段；末尾 xor_size 字节每字节 ^0x88。
"""
import glob
import hashlib
import json
import os
import re
import sys

from Crypto.Cipher import AES

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from core import db, messages  # noqa: E402

SIG = b"\x07\x08V2\x08\x07"


def img_key():
    try:
        return json.load(open(config.keys_json())).get("_img_key")
    except Exception:  # noqa: BLE001
        return None


def account_dir():
    d = config.db_storage_dir()          # 未登录时为 None
    return os.path.dirname(d) if d else None


def _key_bytes(key):
    if isinstance(key, bytes):
        return key
    if key.startswith("hex:"):
        return bytes.fromhex(key[4:])
    if len(key) == 32 and all(c in "0123456789abcdefABCDEF" for c in key):
        return bytes.fromhex(key)
    return key.encode()          # 16 字节 ASCII


def decrypt_dat(data, key):
    """V2 结构(实测,经注入微信解密例程逆向确认):
      [6B magic][u32 aes_size][u32 xor_size][1B]  ← 共 15 字节头
      + AES段(从偏移 15 起, aes_size 字节, AES-128-ECB 全局图片密钥, 含 JPEG 头)
      + 明文中段
      + XOR段(末 xor_size 字节, 每字节 ^ xor_key; xor_key = 末字节 ^ 0xD9, 即 JPEG 尾 0xD9)
    密钥为账号级 16 字节(实测是 ASCII 串), 由注入抓取一次后存 keys.json。
    """
    if data[:6] != SIG:
        return None
    aes_size = int.from_bytes(data[6:10], "little")
    xor_size = int.from_bytes(data[10:14], "little")
    body = data[15:]                                    # AES 段从文件偏移 15 开始
    n = (aes_size // 16) * 16
    k = _key_bytes(key)
    try:
        dec = AES.new(k, AES.MODE_ECB).decrypt(body[:n])
    except ValueError:
        return None
    out = bytearray(dec[:aes_size])
    out += body[aes_size:len(body) - xor_size]          # 明文中段
    if xor_size:
        xk = data[-1] ^ 0xD9
        out += bytes(b ^ xk for b in body[len(body) - xor_size:])
    return bytes(out)


_MD5_RE = re.compile(r'md5\s*=\s*"([0-9a-fA-F]{32})"')


def _msg_img_md5(chat_username, local_id):
    table = "Msg_" + hashlib.md5(chat_username.encode()).hexdigest()
    con = db.connect("message")
    try:
        cols = set(db.columns(con, table))
        ctc = "WCDB_CT_message_content" if "WCDB_CT_message_content" in cols else None
        sel = "message_content" + (f",{ctc}" if ctc else "")
        r = con.execute(f"SELECT {sel} FROM {table} WHERE local_id=?", (local_id,)).fetchone()
    finally:
        con.close()
    if not r:
        return None
    content = messages._decompress(r[0], r[1] if ctc else None)
    _, content = messages._split_group_sender(content)
    mds = _MD5_RE.findall(content or "")
    return mds[0].lower() if mds else None


def _dat_filename(md5):
    """从 hardlink.db 查图片 md5 对应的本地 .dat 文件名。"""
    try:
        con = db.connect("hardlink")
    except FileNotFoundError:
        return None
    try:
        for tbl in ("image_hardlink_info_v4",):
            if not db.table_exists(con, tbl):
                continue
            r = con.execute(f"SELECT file_name FROM {tbl} WHERE md5=?", (md5,)).fetchone()
            if r and r[0]:
                return r[0]
    finally:
        con.close()
    return None


def _resource_basehash(chat_username, local_id):
    """从 message_resource.db 的 MessageResourceInfo 取该图的本地文件名 hash。
    这是最全的映射(覆盖 1000+ 媒体消息)，远超 hardlink(仅百余条)。"""
    try:
        con = db.connect("msgres")
    except Exception:  # noqa: BLE001
        return None
    try:
        if not db.table_exists(con, "MessageResourceInfo"):
            return None
        cid = con.execute("SELECT rowid FROM ChatName2Id WHERE user_name=?",
                          (chat_username,)).fetchone()
        if not cid:
            return None
        r = con.execute(
            "SELECT packed_info FROM MessageResourceInfo "
            "WHERE chat_id=? AND message_local_id=? AND message_local_type=3",
            (cid[0], local_id)).fetchone()
        if not r or not r[0]:
            return None
        m = re.search(rb"[0-9a-f]{32}", bytes(r[0]))   # packed_info 里的文件名 hash
        return m.group().decode() if m else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        con.close()


def _find_dat_by_hash(h):
    """按本地文件名 hash 找加密 .dat。优先大图 _b.dat，其次缩略图 _t.dat。"""
    acc = account_dir()
    if not acc or not h:
        return None
    for pat in (
        "/cache/**/Bubble/" + h + "_b.dat",         # 大图(全分辨率)
        "/cache/**/Bubble/" + h + ".dat",
        "/msg/attach/**/Img/" + h + ".dat",         # 缩略图
        "/msg/attach/**/Img/" + h + "_t.dat",
    ):
        hits = glob.glob(acc + pat, recursive=True)
        if hits:
            return hits[0]
    return None


def _find_dat(md5):
    """兜底：仅有 md5 时经 hardlink 映射找 .dat（覆盖少）。"""
    fn = _dat_filename(md5)
    if fn:
        h = re.sub(r"_[tbh]$", "", re.sub(r"\.dat$", "", os.path.basename(fn), flags=re.I))
        p = _find_dat_by_hash(h)
        if p:
            return p
    return _find_dat_by_hash(md5)


def is_image_msg(chat_username, local_id):
    """该消息在本地是否仍有可解密的图片 .dat 或已缓存(用于撤回图片的探测：能显示才认)。"""
    try:
        if os.path.exists(_revoke_cache_path(chat_username, local_id)):
            return True
        h = _resource_basehash(chat_username, local_id)
        return bool(h and _find_dat_by_hash(h))
    except Exception:  # noqa: BLE001
        return False


def _revoke_cache_path(chat_username, local_id):
    acc = account_dir() or "/tmp"
    d = os.path.join(acc, "revokecache")
    key = hashlib.md5(chat_username.encode()).hexdigest()[:12]
    return os.path.join(d, f"{key}_{local_id}.jpg")


def _decrypt_from_dat(chat_username, local_id):
    """走主路径(资源映射→.dat→解密)返回明文图 bytes，仅用于缓存(不含 temp 兜底)。"""
    key = img_key()
    if not key:
        return None
    path = _find_dat_by_hash(_resource_basehash(chat_username, local_id))
    if not path:
        md5 = _msg_img_md5(chat_username, local_id)
        if md5:
            path = _find_dat(md5)
    if not path:
        return None
    img = decrypt_dat(open(path, "rb").read(), key)
    if img and (img[:3] == b"\xff\xd8\xff" or img[:8] == b"\x89PNG\r\n\x1a\n"
                or img[:4] in (b"GIF8", b"RIFF")):
        return img
    return None


def _looks_thumb(data):
    """粗判是否只是缩略图(小)：优先按像素尺寸，缺 PIL 时按字节数。"""
    if not data:
        return True
    try:
        from PIL import Image
        import io
        w, h = Image.open(io.BytesIO(data)).size
        return max(w, h) < 400            # 微信正常图短边~500+，<400 基本是缩略图
    except Exception:  # noqa: BLE001
        return len(data) < 20000          # 无 PIL：<20KB 视为缩略图


def cache_image(chat_username, local_id, upgrade=False):
    """收到图片时先解密缓存一份到 revokecache/，即便之后被撤回、微信删了本地 .dat 仍能显示。
    已缓存则跳过；upgrade=True 时若现在能拿到更清晰(更大)的版本则覆盖旧缓存。
    返回 (是否已就绪, 是否仍是缩略图)。"""
    p = _revoke_cache_path(chat_username, local_id)
    existing = None
    if os.path.exists(p):
        if not upgrade:
            try:
                return True, _looks_thumb(open(p, "rb").read())
            except Exception:  # noqa: BLE001
                return True, False
        try:
            existing = open(p, "rb").read()
        except Exception:  # noqa: BLE001
            existing = None
    img = _decrypt_from_dat(chat_username, local_id)      # 优先 _b.dat(全分辨率)
    if not img:
        return (existing is not None), _looks_thumb(existing) if existing else True
    # 已有缓存且新解出的不比旧的大，就别覆盖(避免用缩略图盖掉已缓存的大图)
    if existing is not None and len(img) <= len(existing):
        return True, _looks_thumb(existing)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(img)
        return True, _looks_thumb(img)
    except Exception:  # noqa: BLE001
        return (existing is not None), True


import threading  # noqa: E402

# 全分辨率抓取：微信收到图只先下缩略图，全图要"打开看"才下载。为让"撤回的图"也清晰，
# 在图还没被撤回时，后台驱动微信打开该会话最近的图→触发下载全图→再升级缓存。
# 每会话去抖，避免频繁翻动微信 UI。
_fr_lock = threading.Lock()
_fr_last = {}              # chat -> 上次抓全图时间
_fr_recent = {}           # chat -> [(lid), ...] 待升级的近期缩略图
_FR_DEBOUNCE = 25.0


def _display_name(chat_username):
    try:
        from core import contacts
        if chat_username.endswith("@chatroom"):
            for g in contacts.list_groups():
                if g["username"] == chat_username:
                    return g.get("name") or chat_username
        else:
            for c in contacts.list_contacts():
                if c["username"] == chat_username:
                    return c.get("name") or chat_username
    except Exception:  # noqa: BLE001
        pass
    return chat_username


def _fullres_worker(chat_username):
    import time as _t
    try:
        from core import harvest
        name = _display_name(chat_username)
        harvest.harvest(name, nav=8, log=lambda *_: None)   # 打开最近的图→微信下全图
        _t.sleep(0.5)
        with _fr_lock:
            lids = list(dict.fromkeys(_fr_recent.pop(chat_username, [])))
        for lid in lids:                                    # 用全图升级缓存
            try:
                cache_image(chat_username, lid, upgrade=True)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


def _fullres_enabled():
    """是否开启"撤回图片抓全图"(默认关：它会驱动微信翻图,noVNC 里会有滚动)。
    在 bot_rules.json 里设 {"fullres_capture": true} 开启。"""
    try:
        import json
        f = os.path.join(config.account_dir(), "bot_rules.json")
        return bool(json.load(open(f, encoding="utf-8")).get("fullres_capture"))
    except Exception:  # noqa: BLE001
        return False


def schedule_fullres(chat_username, local_id):
    """登记一张缩略图、去抖地后台抓全图并升级缓存(供撤回后仍清晰)。默认关，见 _fullres_enabled。"""
    import time as _t
    if not _fullres_enabled():
        return
    with _fr_lock:
        _fr_recent.setdefault(chat_username, []).append(local_id)
        if _t.time() - _fr_last.get(chat_username, 0) < _FR_DEBOUNCE:
            return
        _fr_last[chat_username] = _t.time()
    threading.Thread(target=_fullres_worker, args=(chat_username,), daemon=True).start()


def images_available():
    """能否显示收到的图：有账号级图片密钥即可(离线解密)，或微信已解密到 temp。"""
    if img_key():
        return True
    acc = account_dir()
    if not acc:                          # 未登录/无数据
        return False
    base = os.path.join(acc, "temp", "ImageUtils")
    try:
        for _root, _dirs, files in os.walk(base):
            if any(f.endswith(".jpg") for f in files):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _mime(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF":
        return "image/webp"
    return "image/jpeg"


def _basehash(md5):
    """图片 md5 → 本地文件基名(hardlink 文件名去掉 .dat 和 _t/_b/_h 后缀)。"""
    fn = _dat_filename(md5)
    if not fn:
        return None
    b = os.path.basename(fn)
    b = re.sub(r"\.dat$", "", b, flags=re.I)
    b = re.sub(r"_[tbh]$", "", b)
    return b


def _temp_jpg(name):
    """在 temp/ImageUtils 里找微信已解密的明文图(它显示图片时会解密到这)。"""
    if not name:
        return None
    base = os.path.join(account_dir(), "temp")
    for pat in (name + ".jpg", name + "*.jpg", name + ".*"):
        hits = glob.glob(os.path.join(base, "**", pat), recursive=True)
        hits = [h for h in hits if os.path.isfile(h)]
        if hits:
            return max(hits, key=os.path.getsize)     # 取最大的(全图而非缩略)
    return None


def get_msg_image(chat_username, local_id):
    """返回 (bytes, mime) 或 (None, reason)。

    主路径：用账号级图片密钥直接离线解密 .dat（AES-128-ECB 段 + XOR 段），
    对任意收到的图都可用、实时、无需在微信里看过、无需注入。
    兜底：微信已解密到 temp/ImageUtils 的明文图。
    """
    # 主路径：离线解密 .dat（message_resource 映射覆盖最全，其次 hardlink/md5）
    key = img_key()
    if key:
        path = _find_dat_by_hash(_resource_basehash(chat_username, local_id))
        if not path:
            md5 = _msg_img_md5(chat_username, local_id)
            if md5:
                path = _find_dat(md5)
        if path:
            img = decrypt_dat(open(path, "rb").read(), key)
            if img and (img[:3] == b"\xff\xd8\xff" or img[:8] == b"\x89PNG\r\n\x1a\n"
                        or img[:4] in (b"GIF8", b"RIFF")):
                return img, _mime(img)
    # 兜底1：撤回前预缓存的明文图(微信可能已删本地 .dat)
    cp = _revoke_cache_path(chat_username, local_id)
    if os.path.exists(cp):
        data = open(cp, "rb").read()
        if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
            return data, _mime(data)
    # 兜底2：微信已解密的明文图
    md5 = _msg_img_md5(chat_username, local_id)
    if not md5:
        return None, "no-md5"
    for name in (_basehash(md5), md5):
        p = _temp_jpg(name)
        if p:
            data = open(p, "rb").read()
            if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
                return data, _mime(data)
    return None, ("no-img-key(点刷新密钥重新抓取)" if not key else "no-dat(该图未下载到本地)")


if __name__ == "__main__":
    print("img_key:", (img_key() or "无")[:6], "…" if img_key() else "")
