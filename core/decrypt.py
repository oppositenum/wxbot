"""解密微信 4.x (WCDB / SQLCipher 变体) 数据库。

规格（见 wechat-colleague/docs/encryption.md）：
- AES-256-CBC，页大小 4096，保留区 80 字节（16B IV + 64B HMAC-SHA512）
- KDF：PBKDF2-HMAC-SHA512，256000 次迭代，salt=文件前 16 字节
- 但内存里已有派生后的 32 字节 key，无需再 KDF —— 直接用 key 做 AES-CBC。
- 第 1 页前 16 字节是 salt（明文），加密数据从 offset 16 开始。

用法： python3 -m core.decrypt          # 增量解密全部已知密钥的库
       python3 -m core.decrypt --force  # 全量重解
"""
import hashlib
import hmac
import json
import os
import sys
import threading

from Crypto.Cipher import AES

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

PAGE = 4096
RESERVE = 80          # 16 IV + 64 HMAC
IV_LEN = 16
HMAC_LEN = 64
SALT_LEN = 16
KDF_ITER = 256000
SQLITE_HEADER = b"SQLite format 3\x00"


def _hmac_salt(salt):
    # SQLCipher 的 HMAC salt = 主 salt 每字节 XOR 0x3a
    return bytes(b ^ 0x3a for b in salt)


def derive_hmac_key(page_key, salt):
    """HMAC 校验用的 key（SQLCipher: 从 hmac_salt 再 PBKDF2 2 轮）。仅用于可选校验。"""
    return hashlib.pbkdf2_hmac("sha512", page_key, _hmac_salt(salt), 2, dklen=64)


def _decrypt_page(key, page, pgno):
    """解密单页（含保留区的原始 4096 字节）→ 4096 字节明文页（保留区置零）。"""
    start = SALT_LEN if pgno == 1 else 0
    reserve_start = PAGE - RESERVE
    iv = page[reserve_start:reserve_start + IV_LEN]
    dec = AES.new(key, AES.MODE_CBC, iv).decrypt(page[start:reserve_start])
    if pgno == 1:
        return SQLITE_HEADER + dec + b"\x00" * RESERVE
    return dec + b"\x00" * RESERVE


def _read_wal_frames(wal_path):
    """解析 SQLCipher/SQLite WAL，返回 {pgno: 原始加密页(4096B)}，后写覆盖先写。"""
    try:
        data = open(wal_path, "rb").read()
    except OSError:
        return {}
    if len(data) < 32 or data[0:4] not in (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83"):
        return {}
    hdr_salt = data[16:24]
    frames = {}
    off, fsize = 32, 24 + PAGE
    while off + fsize <= len(data):
        fh = data[off:off + 24]
        pgno = int.from_bytes(fh[0:4], "big")
        salt = fh[8:16]
        if salt != hdr_salt or pgno == 0:      # 到达无效/旧帧，停止
            break
        frames[pgno] = data[off + 24:off + 24 + PAGE]
        off += fsize
    return frames


def decrypt_db(src_path, key_hex, dst_path, verify_hmac=False):
    key = bytes.fromhex(key_hex)
    with open(src_path, "rb") as f:
        blob = f.read()
    if len(blob) < PAGE:
        raise ValueError(f"{src_path} 太小，非法数据库")

    npages = len(blob) // PAGE
    pages = [None] * npages
    for i in range(npages):
        pages[i] = _decrypt_page(key, blob[i * PAGE:(i + 1) * PAGE], i + 1)

    # 叠加 WAL 里更新的页（实时，无需等 checkpoint）
    frames = _read_wal_frames(src_path + "-wal")
    for pgno, encpage in frames.items():
        if len(encpage) < PAGE:
            continue
        plain = _decrypt_page(key, encpage, pgno)
        if pgno - 1 < len(pages):
            pages[pgno - 1] = plain
        else:
            pages.extend([b"\x00" * PAGE] * (pgno - 1 - len(pages)))
            pages.append(plain)

    out = bytearray(b"".join(pages))
    out[18], out[19] = 1, 1          # 读写版本=1(回滚日志),否则 sqlite 以为是 WAL 库要找 -wal
    out[20] = RESERVE
    out[21], out[22], out[23] = 64, 32, 32

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    tmp = dst_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(out)
    # 校验：微信正在写 db+wal 时可能读到不一致快照，产出损坏库。
    # 校验通过才替换,否则丢弃 tmp、保留上一份可用的解密库。
    import sqlite3 as _sq
    try:
        c = _sq.connect(f"file:{tmp}?mode=ro", uri=True)
        c.execute("SELECT count(*) FROM sqlite_master").fetchone()
        c.close()
    except Exception as e:  # noqa: BLE001
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ValueError(f"解密产物校验失败(微信同步中,已保留旧库): {e}")
    os.replace(tmp, dst_path)


def load_keys():
    if not os.path.exists(config.keys_json()):
        raise FileNotFoundError(
            f"未找到 {config.keys_json()}，请先运行： sudo python3 -m core.keys"
        )
    with open(config.keys_json()) as f:
        return json.load(f)


# 每个解密库一把锁(按目标路径):同一个库的 .tmp 不能被两个线程同时写,
# 但不同库互不相干——别让一个解不出来的库(比如密钥过期)把读消息也堵住。
_locks_guard = threading.Lock()
_locks = {}
# 解密失败的库:{目标路径: (失败时的源库时间戳, 失败时间)}。源库没变就先别重试,
# 免得每次调用都白跑一遍全库解密 + 重试。
_failed = {}
FAIL_BACKOFF = 60          # 源库没变时,失败后至少隔这么久再试(秒)
RETRIES = 4                # 微信同步中易读到不一致快照 → 重试几次拿到一致快照


def _lock_for(dst):
    with _locks_guard:
        return _locks.setdefault(dst, threading.RLock())


def _src_stamp(src):
    """库与 -wal 的 (最新 mtime, 大小)。新消息常只改 -wal。"""
    stamp = [os.path.getmtime(src), os.path.getsize(src)]
    wal = src + "-wal"
    if os.path.exists(wal):
        stamp[0] = max(stamp[0], os.path.getmtime(wal))
        stamp[1] += os.path.getsize(wal)
    return tuple(stamp)


def _refresh_one(src, key_hex, dst, force):
    import time as _t
    with _lock_for(dst):
        stamp = _src_stamp(src)
        if not force and os.path.exists(dst) and os.path.getmtime(dst) >= stamp[0]:
            return "cached"
        fail = _failed.get(dst)
        if not force and fail and fail[0] == stamp and _t.time() - fail[1] < FAIL_BACKOFF:
            return "stale-kept" if os.path.exists(dst) else "err:backoff"
        last_err = None
        for attempt in range(RETRIES):
            try:
                decrypt_db(src, key_hex, dst)
                _failed.pop(dst, None)
                return "ok"
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt + 1 < RETRIES:
                    _t.sleep(0.25)
        _failed[dst] = (_src_stamp(src), _t.time())
        if os.path.exists(dst):
            return "stale-kept"      # 保留上一份可用库
        return f"err:{last_err}"


def run(force=False, only=None):
    """解密核心库（config.CORE_DBS）。only 可指定 key 子集。"""
    config.ensure_dirs()
    keys = load_keys()
    db_storage = config.db_storage_dir()
    targets = config.CORE_DBS.items()
    if only:
        targets = [(k, v) for k, v in targets if k in only]

    results = {}
    for key_name, rel in targets:
        src = os.path.join(db_storage, rel)
        dst = config.decrypted_path(key_name)
        if rel not in keys:
            results[key_name] = "no-key"
            continue
        if not os.path.exists(src):
            results[key_name] = "no-src"
            continue
        results[key_name] = _refresh_one(src, keys[rel], dst, force)
    return results


def main():
    force = "--force" in sys.argv
    res = run(force=force)
    for k, v in res.items():
        print(f"  {k:12} {v}")
    ok = [k for k, v in res.items() if v in ("ok", "cached")]
    print(f"\n{len(ok)}/{len(res)} 核心库就绪：{config.decrypted_dir()}")


if __name__ == "__main__":
    main()
