"""解密/转码 微信语音(type=34) 与 定位视频(type=43)，供网页播放。

语音(type=34)：
    - 明文 SILK v3，存在 media_0.db 的 VoiceInfo.voice_data（未加密，无需密钥）。
    - blob 带 1 字节 0x02 前缀，剥掉后即 `#!SILK_V3...`。
    - 用 pilk 把 SILK→PCM(s16le mono 24kHz)，再 ffmpeg→MP3（无 ffmpeg 则内置 wave→WAV）。
    - 覆盖率：全量。每条收到的语音微信都会存进库，与"是否点开听过"无关。

视频(type=43)：
    - 完整视频只在腾讯 CDN（cdnvideourl，用 aeskey 加密），本地默认没有明文 mp4。
    - 微信在窗口里"点开播放"某视频后，会把明文写到
        msg/video/<yyyy-mm>/<base>.mp4          （完整视频）
        msg/video/<yyyy-mm>/<base>_thumb.jpg    （缩略图，收到即有）
      base 取自 message_resource.db → MessageResourceInfo.packed_info。
    - 所以：缩略图基本全量可用（可作 <video poster>）；完整 mp4 只有"播放过的"才有，
      逻辑与图片"看过才有明文"一致。取不到完整视频时返回 None + 原因。
"""
import glob
import hashlib
import os
import shutil
import subprocess
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from core import db  # noqa: E402

# media_0.db / message_resource.db 需先解密到 decrypted/ 下。
# 它们已加入 config.CORE_DBS，由 server 的 poller 定期刷新；此处再兜底按需解密。
_MEDIA_KEY = "media"          # -> message/media_0.db
_MSGRES_KEY = "msgres"        # -> message/message_resource.db


def account_dir():
    return os.path.dirname(config.db_storage_dir())


def _cache_dir():
    d = os.path.join(config.account_dir(), "mediacache")
    os.makedirs(d, exist_ok=True)
    return d


def _ensure_decrypted(key):
    """确保 decrypted/<key>.db 存在（缺失时按需解密一次）。返回路径或 None。"""
    path = config.decrypted_path(key)
    if os.path.exists(path):
        return path
    try:
        from core import decrypt
        decrypt.run(only=[key])
    except Exception:  # noqa: BLE001
        pass
    return path if os.path.exists(path) else None


def _ro(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ---------------- 语音 ----------------
def _silk_available():
    try:
        import pilk  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _voice_row(chat_username, local_id):
    """从 media_0.db 取 (svr_id, voice_data)。按 (chat_name_id, local_id) 定位。"""
    path = _ensure_decrypted(_MEDIA_KEY)
    if not path:
        return None
    con = _ro(path)
    try:
        if not db.table_exists(con, "VoiceInfo"):
            return None
        r = con.execute(
            "SELECT rowid FROM Name2Id WHERE user_name=?", (chat_username,)
        ).fetchone()
        if not r:
            return None
        cid = r[0]
        row = con.execute(
            "SELECT svr_id, voice_data FROM VoiceInfo "
            "WHERE chat_name_id=? AND local_id=?", (cid, local_id)
        ).fetchone()
        if not row:
            return None
        return row[0], row[1]
    finally:
        con.close()


def _silk_to_audio(silk):
    """SILK bytes → (bytes, mime)。优先 mp3（需 ffmpeg），否则 wav（纯 python）。"""
    import pilk
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "a.silk")
        pcmp = os.path.join(td, "a.pcm")
        with open(sp, "wb") as f:
            f.write(silk)
        pilk.decode(sp, pcmp, pcm_rate=24000)
        pcm = open(pcmp, "rb").read()
        ff = shutil.which("ffmpeg")
        if ff:
            mp3 = os.path.join(td, "a.mp3")
            rc = subprocess.run(
                [ff, "-y", "-loglevel", "error", "-f", "s16le", "-ar", "24000",
                 "-ac", "1", "-i", pcmp, "-b:a", "32k", mp3],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            ).returncode
            if rc == 0 and os.path.exists(mp3):
                return open(mp3, "rb").read(), "audio/mpeg"
        # 兜底：包成 WAV
        import io
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
        return buf.getvalue(), "audio/wav"


def get_msg_voice(chat_username, local_id):
    """返回 (bytes, mime) 或 (None, reason)。mime 为 audio/mpeg 或 audio/wav。"""
    if not _silk_available():
        return None, "no-silk-decoder(pip install pilk)"
    row = _voice_row(chat_username, int(local_id))
    if not row:
        return None, "no-voice-data"
    svr_id, blob = row
    if not blob:
        return None, "empty-voice"
    # 命中缓存
    tag = str(svr_id) if svr_id else hashlib.md5(
        f"{chat_username}:{local_id}".encode()).hexdigest()
    for ext, mime in ((".mp3", "audio/mpeg"), (".wav", "audio/wav")):
        cp = os.path.join(_cache_dir(), "voice_" + tag + ext)
        if os.path.exists(cp):
            return open(cp, "rb").read(), mime
    silk = blob[1:] if blob[:1] == b"\x02" else blob
    if silk[:9] != b"#!SILK_V3":
        return None, "not-silk"
    try:
        data, mime = _silk_to_audio(silk)
    except Exception as e:  # noqa: BLE001
        return None, f"decode-error:{e}"
    ext = ".mp3" if mime == "audio/mpeg" else ".wav"
    try:
        with open(os.path.join(_cache_dir(), "voice_" + tag + ext), "wb") as f:
            f.write(data)
    except Exception:  # noqa: BLE001
        pass
    return data, mime


def voice_available():
    """media_0.db 里是否有语音（有就说明语音功能可用）。"""
    path = config.decrypted_path(_MEDIA_KEY)
    if not os.path.exists(path):
        return False
    try:
        con = _ro(path)
        try:
            if not db.table_exists(con, "VoiceInfo"):
                return False
            return con.execute("SELECT 1 FROM VoiceInfo LIMIT 1").fetchone() is not None
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return False


# ---------------- 视频 ----------------
def _video_base(chat_username, local_id):
    """从 message_resource.db 取视频本地文件基名(hash)。"""
    path = _ensure_decrypted(_MSGRES_KEY)
    if not path:
        return None
    con = _ro(path)
    try:
        if not db.table_exists(con, "MessageResourceInfo"):
            return None
        r = con.execute(
            "SELECT rowid FROM ChatName2Id WHERE user_name=?", (chat_username,)
        ).fetchone()
        if not r:
            return None
        cid = r[0]
        row = con.execute(
            "SELECT packed_info FROM MessageResourceInfo "
            "WHERE chat_id=? AND message_local_id=? AND message_local_type=43",
            (cid, local_id)
        ).fetchone()
        if not row or not row[0]:
            return None
        return _base_from_packed(row[0])
    finally:
        con.close()


def _base_from_packed(pi):
    """packed_info 是 protobuf: field2{ field1: <32字节hex串> }。取出该 32 字节基名。"""
    from core import protobuf
    try:
        f = protobuf.decode(pi)
        sub = f.get(2, [{}])[0].get("value")
        if isinstance(sub, (bytes, bytearray)):
            inner = protobuf.decode(sub)
            v = inner.get(1, [{}])[0].get("value")
            if isinstance(v, (bytes, bytearray)):
                s = v.decode("ascii", "ignore")
                if len(s) == 32 and all(c in "0123456789abcdef" for c in s):
                    return s
    except Exception:  # noqa: BLE001
        pass
    # 兜底：直接扫 0x0a 0x20 后 32 字节
    i = pi.find(b"\x0a\x20")
    if i >= 0:
        s = pi[i + 2:i + 2 + 32].decode("ascii", "ignore")
        if len(s) == 32 and all(c in "0123456789abcdef" for c in s):
            return s
    return None


def _video_root():
    return os.path.join(account_dir(), "msg", "video")


def get_msg_video(chat_username, local_id):
    """返回 (bytes, 'video/mp4') 或 (None, reason)。

    仅当微信已把明文 mp4 下载到本地(播放过该视频)时才有；否则返回 None。
    """
    base = _video_base(chat_username, int(local_id))
    if not base:
        return None, "no-resource-mapping"
    hits = glob.glob(os.path.join(_video_root(), "*", base + ".mp4"))
    hits = [h for h in hits if os.path.isfile(h) and os.path.getsize(h) > 0]
    if not hits:
        return None, "not-downloaded(在微信窗口里点开播放该视频后即可)"
    data = open(max(hits, key=os.path.getsize), "rb").read()
    if b"ftyp" not in data[:32]:
        return None, "invalid-mp4"
    return data, "video/mp4"


def get_msg_video_thumb(chat_username, local_id):
    """返回视频缩略图 (bytes, 'image/jpeg') 或 (None, reason)。可作 <video poster>。"""
    base = _video_base(chat_username, int(local_id))
    if not base:
        return None, "no-resource-mapping"
    hits = glob.glob(os.path.join(_video_root(), "*", base + "_thumb.jpg"))
    hits = [h for h in hits if os.path.isfile(h) and os.path.getsize(h) > 0]
    if not hits:
        return None, "no-thumb"
    return open(max(hits, key=os.path.getsize), "rb").read(), "image/jpeg"


if __name__ == "__main__":
    print("silk decoder:", _silk_available(), " ffmpeg:", bool(shutil.which("ffmpeg")))
    print("voice_available:", voice_available())
