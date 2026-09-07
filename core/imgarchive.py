"""图片永久存档：把收到的图解密成高清图存到 accounts/<wxid>/imgarchive/，并记索引。

目的：即使对方撤回、或本地 .dat 被清，也能永久留存高清原图，且可事后用视觉模型读图内容
(存 description 便于检索/让机器人"知道图片说了啥")。
- archive_image：解出【本地能拿到的最清晰版】存档；已存且新版更大则升级(如后续注入补下了原图)。
- index.jsonl：每行一条 {chat, local_id, ts, md5, w, h, thumb, path, desc}。
清晰度依赖图是否已下全图——配合"保持会话打开"/路线A注入触发下载,能拿到高清/原图。
"""
import hashlib
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from core import imgdec  # noqa: E402


def archive_dir():
    d = os.path.join(config.account_dir(), "imgarchive")
    os.makedirs(d, exist_ok=True)
    return d


def _index_path():
    return os.path.join(archive_dir(), "index.jsonl")


def _key(chat, local_id):
    return hashlib.md5(chat.encode()).hexdigest()[:12] + f"_{local_id}"


def _img_path(chat, local_id):
    return os.path.join(archive_dir(), _key(chat, local_id) + ".jpg")


def _dims(data):
    try:
        from PIL import Image
        return Image.open(io.BytesIO(data)).size
    except Exception:  # noqa: BLE001
        return (0, 0)


def is_archived(chat, local_id):
    return os.path.exists(_img_path(chat, local_id))


def archived_bytes(chat, local_id):
    p = _img_path(chat, local_id)
    if os.path.exists(p):
        return open(p, "rb").read()
    return None


def _load_index():
    out = {}
    p = _index_path()
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            try:
                e = json.loads(line)
                out[e["k"]] = e
            except Exception:  # noqa: BLE001
                pass
    return out


def _save_index(idx):
    with open(_index_path(), "w", encoding="utf-8") as f:
        for e in idx.values():
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def archive_image(chat, local_id, data=None, meta=None):
    """存档一张图(取本地最清晰版;已存且新版更大才覆盖)。返回 {ok, path, thumb, w, h, upgraded}。"""
    if data is None:
        data, _ = imgdec.get_msg_image(chat, local_id)   # 内部已取最清晰(_best_image/temp)
    if not data:
        return {"ok": False, "reason": "no-image"}
    w, h = _dims(data)
    p = _img_path(chat, local_id)
    upgraded = False
    if os.path.exists(p):
        try:
            old = open(p, "rb").read()
            if len(data) <= len(old):                    # 新的不更大就不覆盖
                return {"ok": True, "path": p, "thumb": max(w, h) < 400 if (w or h) else None,
                        "w": w, "h": h, "upgraded": False, "kept": True}
            upgraded = True
        except Exception:  # noqa: BLE001
            pass
    with open(p, "wb") as f:
        f.write(data)
    idx = _load_index()
    k = _key(chat, local_id)
    entry = idx.get(k, {})
    entry.update({"k": k, "chat": chat, "local_id": local_id, "path": p,
                  "md5": hashlib.md5(data).hexdigest(), "w": w, "h": h,
                  "thumb": bool((w or h) and max(w, h) < 400),
                  "ts": int(time.time()), "size": len(data)})
    if meta:
        entry.update({mk: meta[mk] for mk in ("sender", "sender_name", "create_time")
                      if mk in meta})
    idx[k] = entry
    _save_index(idx)
    return {"ok": True, "path": p, "thumb": entry["thumb"], "w": w, "h": h, "upgraded": upgraded}


def describe(chat, local_id, force=False):
    """用视觉模型读存档图的内容,写进索引 desc(让以后能'知道图片说了啥',可检索)。"""
    from core import llm
    idx = _load_index()
    k = _key(chat, local_id)
    e = idx.get(k)
    if not e:
        return None
    if e.get("desc") and not force:
        return e["desc"]
    data = archived_bytes(chat, local_id)
    if not data or not llm.available():
        return None
    d = llm.describe_image(data, media_type="image/jpeg",
                           prompt="详细客观地描述这张图片的内容(是什么、有什么文字/信息),一两句话。")
    if d:
        e["desc"] = d
        idx[k] = e
        _save_index(idx)
    return d


def list_archive(limit=300, with_desc=False):
    idx = _load_index()
    items = sorted(idx.values(), key=lambda e: e.get("ts", 0), reverse=True)[:limit]
    return {"total": len(idx), "items": items}


def search(query, k=20):
    """按描述/发送者检索存档图片(需已 describe)。"""
    q = (query or "").lower()
    hits = []
    for e in _load_index().values():
        blob = (e.get("desc", "") + " " + e.get("sender_name", "")).lower()
        if q and q in blob:
            hits.append(e)
    return sorted(hits, key=lambda e: e.get("ts", 0), reverse=True)[:k]


def stats():
    idx = _load_index()
    thumbs = sum(1 for e in idx.values() if e.get("thumb"))
    described = sum(1 for e in idx.values() if e.get("desc"))
    return {"total": len(idx), "hd": len(idx) - thumbs, "thumb": thumbs, "described": described}
