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
    """V2 结构(实测): [6B magic][u32 aes_size][u32 xor_size] + AES段 + 明文段 + XOR段。
    - AES段 = 前 aes_size 字节，AES-128-ECB(图片密钥)。含 JPEG 头。
    - XOR段 = 末 xor_size 字节，每字节 ^ xor_key；xor_key 由末字节推(JPEG 尾 FFD9)。
    """
    if data[:6] != SIG:
        return None
    aes_size = int.from_bytes(data[6:10], "little")
    xor_size = int.from_bytes(data[10:14], "little")
    body = data[14:]
    n = ((aes_size + 15) // 16) * 16
    k = _key_bytes(key)
    try:
        dec = AES.new(k, AES.MODE_ECB).decrypt(body[:n])
    except ValueError:
        return None
    out = bytearray(dec[:aes_size])
    out += body[aes_size:len(body) - xor_size]          # 明文中段
    if xor_size:
        xk = data[-1] ^ 0xD9                            # 末字节 ^ 0xD9(=JPEG的0xD9)
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


def _find_dat(md5):
    base = os.path.join(account_dir(), "msg", "attach")
    fn = _dat_filename(md5)
    names = [fn] if fn else []
    names += [md5 + ".dat", md5 + "_t.dat"]     # 兜底：直接用 md5 命名
    for n in names:
        hits = glob.glob(os.path.join(base, "**", "Img", n), recursive=True)
        if hits:
            return hits[0]
    return None


def images_available():
    """微信是否已把明文图解密到 temp/ImageUtils(有就说明能显示收到的图)。"""
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

    不再依赖图片密钥(取不到)——微信显示图片时会把明文解密到
    temp/ImageUtils/<hash>.jpg，直接读它。按 消息md5→hardlink文件名→temp 定位。
    """
    md5 = _msg_img_md5(chat_username, local_id)
    if not md5:
        return None, "no-md5"
    # 主路径：微信已解密的明文图
    for name in (_basehash(md5), md5):
        p = _temp_jpg(name)
        if p:
            data = open(p, "rb").read()
            if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
                return data, _mime(data)
    # 兜底：有图片密钥时直接解 .dat（当前一般没有）
    key = img_key()
    if key:
        path = _find_dat(md5)
        if path:
            img = decrypt_dat(open(path, "rb").read(), key)
            if img:
                return img, _mime(img)
    return None, "not-decrypted(在微信窗口里滚动看过该图后即可显示)"


if __name__ == "__main__":
    print("img_key:", (img_key() or "无")[:6], "…" if img_key() else "")
