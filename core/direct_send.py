"""直发(实验):Frida 附加本容器微信,直接调用底层 SendMessage,不经过界面。

只供后台「直发测试」页手动使用,不接入 bot 发送链路。
- 只适配已逆向的微信版本,二进制 sha256 不符就拒绝加载(偏移会错,乱调会崩)。
- 后台进程与微信同为 wechat 用户,可直接附加,不需要 root。
- 会话常驻;微信重启后 PID 变化会自动重新附加。
- 同一时刻只发一条:agent 的全局槽位是共享的。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time

import config

WECHAT_BIN = os.environ.get("WXBOT_WECHAT_BIN", "/opt/wechat/wechat")
from core.direct_send_profiles import PROFILES

SUPPORTED_SHA256 = {sha: p["name"] for sha, p in PROFILES.items()}
# /home/wechat 在数据卷里,装在这里重建容器也不丢;/tmp/fr 是早期手动装的位置
FRIDA_PATHS = [p for p in (os.environ.get("WXBOT_FRIDA_PATH"), "/home/wechat/.wxbot/frida", "/tmp/fr",
                           os.path.join(config.WORK_DIR, "frida")) if p]
AGENT_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "direct_send_agent.js")
HISTORY = os.path.join(config.WORK_DIR, "direct_send_history.jsonl")
MAX_CHARS = 2000
WAIT_SECONDS = 8.0

# 这些错误码出现时,消息一定没有交给微信(可安全改走其它方式);其余(timeout/frida_error)结果不确定。
NOT_SENT_CODES = frozenset({
    "no_frida", "no_wechat", "unsupported", "not_running", "attach_failed", "agent_error",
    "not_logged_in", "request_mismatch", "bad_chat", "empty", "too_long", "no_file",
    "bad_image", "bad_video", "bad_audio", "no_ffmpeg", "convert_failed", "tts_failed", "bad_param", "too_large", "step_failed",
    "not_found", "not_mine", "not_sent_ok", "too_old", "already_revoked", "revoke_unsupported",
    "voice_unsupported", "no_silk_encoder", "encode_failed", "at_unsupported", "quote_unsupported", "bad_at",
})

AT_SEP = "\u2005"   # 微信 @名字 后面的四分之一空格(界面发 @ 实测)

REVOKE_WINDOW = 120   # 微信普通消息 2 分钟内可撤回(服务端最终说了算)

STEP_TEXT = {
    1: "取服务对象失败(第1步)", 2: "取服务对象失败(第2步)", 3: "取服务对象失败(第3步)",
    4: "服务对象类型不符,可能版本不对", 5: "微信库里找不到这条消息(local_id 不对?)",
    6: "取撤回服务失败", 7: "撤回服务类型不符,可能版本不对", 9: "已交给微信发送",
}

_lock = threading.Lock()
_state = {"pid": None, "session": None, "script": None}
_hash_cache = {}


class DirectSendError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _import_frida():
    try:
        import frida  # noqa: F401
        return frida
    except ImportError:
        pass
    for p in FRIDA_PATHS:
        if os.path.isdir(os.path.join(p, "frida")) and p not in sys.path:
            sys.path.append(p)
    try:
        import frida
        return frida
    except ImportError:
        return None


def binary_version():
    """返回 (sha256, 版本名或 None)。按 mtime+size 缓存,130MB 只算一次。"""
    try:
        st = os.stat(WECHAT_BIN)
    except OSError:
        return None, None
    key = (st.st_mtime_ns, st.st_size)
    if _hash_cache.get("key") != key:
        h = hashlib.sha256()
        with open(WECHAT_BIN, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _hash_cache.update(key=key, sha=h.hexdigest())
    sha = _hash_cache["sha"]
    return sha, SUPPORTED_SHA256.get(sha)


def _wechat_pid(frida):
    dev = frida.get_local_device()
    pids = [p.pid for p in dev.enumerate_processes() if p.name == "wechat"]
    return dev, (min(pids) if pids else None)


def _drop():
    sess = _state.get("session")
    _state.update(pid=None, session=None, script=None)
    if sess is not None:
        try:
            sess.detach()
        except Exception:
            pass


def _ensure_script():
    frida = _import_frida()
    if frida is None:
        raise DirectSendError("no_frida", "容器里没有 frida(查找路径:%s)" % ", ".join(FRIDA_PATHS))
    sha, ver = binary_version()
    if sha is None:
        raise DirectSendError("no_wechat", "找不到微信程序 %s" % WECHAT_BIN)
    if ver is None:
        raise DirectSendError("unsupported", "微信版本未适配(sha256 %s…),直发已禁用" % sha[:12])
    dev, pid = _wechat_pid(frida)
    if pid is None:
        _drop()
        raise DirectSendError("not_running", "微信没有在运行")
    if _state["script"] is not None and _state["pid"] == pid and not _state["session"].is_detached:
        return _state["script"]
    _drop()
    with open(AGENT_JS, encoding="utf-8") as f:
        source = "const PROFILE = " + json.dumps(PROFILES[sha]) + ";\n" + f.read()
    session = dev.attach(pid)
    try:
        script = session.create_script(source)
        errors = []
        script.on("message", lambda m, _d: errors.append(m.get("description") or str(m))
                  if m.get("type") == "error" else None)
        script.load()
        if errors:
            raise DirectSendError("agent_error", "agent 加载失败:" + errors[0])
    except BaseException:
        try:
            session.detach()
        except Exception:
            pass
        raise
    _state.update(pid=pid, session=session, script=script)
    return script


def status():
    with _lock:
        out = {"frida": _import_frida() is not None}
        sha, ver = binary_version()
        out.update(sha256=sha, version=ver, supported=bool(ver))
        try:
            script = _ensure_script()
            out.update(script.exports_sync.ready())
            out["pid"] = _state["pid"]
        except DirectSendError as e:
            out.update(attached=False, logged_in=False, error=e.message, code=e.code)
        except Exception as e:  # frida 异常:附加失败/进程刚退出
            _drop()
            out.update(attached=False, logged_in=False, error=f"附加失败:{e}", code="attach_failed")
        return out


def _record(row):
    try:
        os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
        with open(HISTORY, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def history(limit=50):
    try:
        with open(HISTORY, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    rows = []
    for line in reversed(lines):
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


IMAGE_EXT = {b"\x89PNG\r\n\x1a\n": ".png", b"\xff\xd8\xff": ".jpg", b"GIF87a": ".gif", b"GIF89a": ".gif"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MEDIA_DIR = os.path.join(config.WORK_DIR, "direct_media")


def _check_chat(chat):
    chat = (chat or "").strip()
    if not chat or any(c.isspace() for c in chat):
        raise DirectSendError("bad_chat", "请选择发送对象")
    return chat


def _image_ext(head):
    for magic, ext in IMAGE_EXT.items():
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


MAX_VIDEO_BYTES = 25 * 1024 * 1024


def _video_ext(head):
    """MP4/MOV 都是 ISO BMFF:第 4~8 字节为 ftyp。"""
    if head[4:8] != b"ftyp":
        return None
    return ".mov" if head[8:10] == b"qt" else ".mp4"


def _audio_ext(head):
    """常见音频的文件头。微信自己的 SILK 语音 ffmpeg 解不了,单独报错。"""
    if head[4:8] == b"ftyp" and head[8:11] in (b"M4A", b"M4B"):
        return ".m4a"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return ".mp3"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav"
    for magic, ext in ((b"OggS", ".ogg"), (b"fLaC", ".flac"), (b"#!AMR", ".amr")):
        if head.startswith(magic):
            return ext
    return None


def _is_silk(head):
    return head.startswith(b"#!SILK") or head.startswith(b"\x02#!SILK")


def media_kind(head):
    """按文件头判断 'image' / 'audio' / 'video' / None。m4a 也是 ftyp,先判音频。"""
    if _image_ext(head):
        return "image"
    if _audio_ext(head):
        return "audio"
    return "video" if _video_ext(head) else None


def stage_video(data, filename=""):
    """同 stage_image,存视频。"""
    if not data:
        raise DirectSendError("empty", "视频是空的")
    if len(data) > MAX_VIDEO_BYTES:
        raise DirectSendError("too_large", "视频超过 25MB")
    ext = _video_ext(data[:16])
    if not ext:
        raise DirectSendError("bad_video", "只支持 MP4 / MOV 视频")
    os.makedirs(MEDIA_DIR, exist_ok=True)
    path = os.path.join(MEDIA_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(4).hex()}{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_AUDIO_SECONDS = 300
FFMPEG = os.environ.get("WXBOT_FFMPEG", "ffmpeg")


def audio_to_video(src, dst):
    """音频 → 「深绿底 + 绿色声波」的 mp4(Linux 版微信发不了语音条,用视频代替)。

    声波随声音动;首帧是纯色卡片,对方看到的封面不会是黑屏。超过 5 分钟截断。"""
    graph = ("color=c=0x1e2a23:s=640x360:r=15[bg];"
             "[0:a]showwaves=s=640x360:mode=cline:rate=15:colors=0x07C160,format=rgba[w];"
             "[bg][w]overlay=shortest=1,format=yuv420p[v]")
    cmd = [FFMPEG, "-nostdin", "-y", "-loglevel", "error", "-i", src, "-t", str(MAX_AUDIO_SECONDS),
           "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
           "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-movflags", "+faststart", "-shortest", dst]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180)
    except FileNotFoundError:
        raise DirectSendError("no_ffmpeg", "容器里没有 ffmpeg,发不了语音")
    except subprocess.TimeoutExpired:
        raise DirectSendError("convert_failed", "音频转视频超时")
    if r.returncode != 0 or not os.path.isfile(dst) or not os.path.getsize(dst):
        err = (r.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
        raise DirectSendError("convert_failed", "音频转视频失败:" + (err[-1] if err else f"rc={r.returncode}"))
    return dst


def stage_audio(data, filename=""):
    """存音频并转成 mp4,返回 mp4 路径(原音频一起留在 direct_media)。"""
    if not data:
        raise DirectSendError("empty", "音频是空的")
    if len(data) > MAX_AUDIO_BYTES:
        raise DirectSendError("too_large", "音频超过 20MB")
    if _is_silk(data[:16]):
        raise DirectSendError("bad_audio", "微信 SILK 语音不能直接发,请先转成 mp3/m4a/wav")
    ext = _audio_ext(data[:16])
    if not ext:
        raise DirectSendError("bad_audio", "只支持 MP3 / M4A / WAV / OGG / FLAC / AMR 音频")
    os.makedirs(MEDIA_DIR, exist_ok=True)
    base = os.path.join(MEDIA_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(4).hex()}")
    with open(base + ext, "wb") as f:
        f.write(data)
    return audio_to_video(base + ext, base + ".voice.mp4")


def stage_media(data, filename=""):
    """图片、视频、音频都行,返回 (kind, path);音频返回 ('voice', 转好的 mp4)。"""
    kind = media_kind(data[:16])
    if kind == "video":
        return "video", stage_video(data, filename)
    if kind == "audio" or _is_silk(data[:16]):
        return "voice", stage_audio(data, filename)
    return "image", stage_image(data, filename)


def stage_image(data, filename=""):
    """把上传的图片字节存成 work/direct_media/<随机名>,返回路径。

    微信是异步上传 CDN 的,交出去的文件要一直留着,所以每次单独存一份。"""
    if not data:
        raise DirectSendError("empty", "图片是空的")
    if len(data) > MAX_IMAGE_BYTES:
        raise DirectSendError("too_large", "图片超过 20MB")
    ext = _image_ext(data[:16])
    if not ext:
        raise DirectSendError("bad_image", "只支持 PNG / JPG / GIF / WEBP 图片")
    os.makedirs(MEDIA_DIR, exist_ok=True)
    path = os.path.join(MEDIA_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(4).hex()}{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


def _dispatch(row, call, wait=WAIT_SECONDS):
    """在锁内调用 agent 并等协程结果;row 会写进历史。"""
    with _lock:
        try:
            try:
                script = _ensure_script()
            except DirectSendError:
                raise
            except Exception as e:  # 附加阶段失败:肯定还没发
                _drop()
                raise DirectSendError("attach_failed", f"附加微信失败:{e}")
            r = call(script.exports_sync)
            if not r.get("ok"):
                code = r.get("error") or "failed"
                msg = {"not_logged_in": "微信还没登录完成(停在登录页?)",
                       "request_mismatch": "请求对象校验失败,未发送",
                       "revoke_unsupported": "当前微信版本还没适配底层撤回",
                       "voice_unsupported": "当前微信版本不能直发语音条",
                       "at_unsupported": "当前微信版本还没适配 @ 提醒",
                       "quote_unsupported": "当前微信版本还没适配引用回复"}.get(code, code)
                raise DirectSendError(code, msg)
            deadline = time.time() + wait
            step, p = 0, {}
            while time.time() < deadline:
                p = script.exports_sync.poll()
                step = p.get("step") or 0
                if step:
                    break
                time.sleep(0.1)
        except DirectSendError as e:
            row.update(ok=False, code=e.code, error=e.message)
            _record(row)
            raise
        except Exception as e:
            _drop()
            row.update(ok=False, code="frida_error", error=str(e))
            _record(row)
            raise DirectSendError("frida_error", f"调用失败:{e}")
    if step == 9 and row.get("kind") == "revoke":
        ok = bool(int((p.get("raw") or ["0"])[0], 16) & 0xFF) if p.get("raw") else bool(p.get("result"))
        row.update(ok=ok, step=step, message="已撤回" if ok else "",
                   **({} if ok else {"code": "revoke_failed", "error": "微信拒绝撤回(超时限或消息状态不对)"}))
    elif step == 9:
        row.update(ok=True, step=step, message=STEP_TEXT[9])
    elif step:
        row.update(ok=False, step=step, code="step_failed", error=STEP_TEXT.get(step, f"step={step}"))
    else:
        row.update(ok=False, step=0, code="timeout", error="等待超时,协程没有执行(微信可能卡住)")
    _record(row)
    if not row["ok"]:
        raise DirectSendError(row["code"], row["error"])
    return row


def _member_names(chat):
    """群成员 wxid → @ 时显示的名字(群昵称优先,其次昵称)。"""
    from core import contacts
    out = {}
    try:
        for m in contacts.group_members(chat) or []:
            w = m.get("wxid") or m.get("username")
            n = (m.get("group_nick") or m.get("name") or m.get("nick_name") or "").strip()
            if w and n:
                out[w] = n
    except Exception:
        pass
    return out


def resolve_mentions(chat, content):
    """群消息里写的「@成员名」→ (规范化后的正文, [wxid])。名字后补微信的 \u2005 分隔符,
    只认得出本群成员的名字(最长匹配),认不出的 @ 原样保留。"""
    if not chat.endswith("@chatroom") or "@" not in content:
        return content, []
    names = sorted(((n, w) for w, n in _member_names(chat).items()), key=lambda x: -len(x[0]))
    out, wxids, i = [], [], 0
    while i < len(content):
        if content[i] == "@":
            hit = next(((n, w) for n, w in names if content.startswith(n, i + 1)), None)
            if hit:
                n, w = hit
                j = i + 1 + len(n)
                if j < len(content) and content[j] in (" ", AT_SEP):
                    j += 1
                out.append("@" + n + AT_SEP)
                if w not in wxids:
                    wxids.append(w)
                i = j
                continue
        out.append(content[i])
        i += 1
    return "".join(out), wxids


def mention_text(chat, at, content):
    """把要 @ 的人补到正文开头(正文里已经写了「@名字」的不重复加)。返回 (正文, [wxid])。"""
    names = _member_names(chat)
    wxids = []
    head = ""
    for w in at:
        if not isinstance(w, str) or not w or any(c.isspace() or c == "," for c in w):
            raise DirectSendError("bad_at", "要 @ 的人不对")
        if w in wxids:
            continue
        wxids.append(w)
        n = names.get(w)
        if not n:
            from core import contacts
            n = ((contacts.all_names().get(w) or {}).get("name") or "").strip()
        if not n:
            raise DirectSendError("bad_at", f"不在这个群里:{w}")
        if ("@" + n) not in content:
            head += "@" + n + AT_SEP
    text, auto = resolve_mentions(chat, head + content)
    return text, wxids + [w for w in auto if w not in wxids]


def _find_message(chat, local_id):
    """解密库里取一条消息(引用用):返回 {localId, createTime, sender, type}。"""
    from core import db, messages
    table = messages.msg_table(chat)
    con = db.connect("message")
    try:
        if not db.table_exists(con, table):
            raise DirectSendError("not_found", "找不到这个会话的消息")
        r = con.execute(f"SELECT local_id, local_type, real_sender_id, create_time FROM {table} WHERE local_id = ?",
                        (int(local_id),)).fetchone()
        id2name = messages._name2id_map(con) if r else {}
    finally:
        con.close()
    if not r:
        raise DirectSendError("not_found", "没找到要引用的消息(库可能还没同步,稍等再试)")
    if r[1] == 10000:
        raise DirectSendError("not_found", "要引用的消息已经撤回了")
    return {"localId": int(r[0]), "type": int(r[1]), "sender": id2name.get(r[2]) or "", "createTime": int(r[3])}


def _sender_name(chat, wxid):
    if not wxid:
        return ""
    if chat.endswith("@chatroom"):
        n = _member_names(chat).get(wxid)
        if n:
            return n
    from core import contacts
    return ((contacts.all_names().get(wxid) or {}).get("name") or "").strip()


def send(chat, content, name="", source="manual", at=None, quote=None):
    """发文字。chat=wxid 或 xxx@chatroom。返回结果字典;失败抛 DirectSendError。
    at:要 @ 的 wxid 列表(群里),"auto" = 只把正文里写的「@成员名」变成真 @;
    quote:被引用消息的 local_id(引用回复,微信 4.1.13+)。"""
    chat = _check_chat(chat)
    content = content if isinstance(content, str) else ""
    if not content.strip():
        raise DirectSendError("empty", "内容不能为空")
    if len(content) > MAX_CHARS:
        raise DirectSendError("too_long", f"内容超过 {MAX_CHARS} 字")
    wxids = []
    if at == "auto":
        if at_supported():
            content, wxids = resolve_mentions(chat, content)
    elif at:
        if not chat.endswith("@chatroom"):
            raise DirectSendError("bad_at", "只有群聊能 @ 人")
        content, wxids = mention_text(chat, list(at), content)
    opts = {}
    if wxids:
        opts["at"] = wxids
    row = {"t": int(time.time()), "kind": "quote" if quote is not None else "text", "chat": chat, "name": name,
           "content": content, "source": source}
    if wxids:
        row["at"] = wxids
    if quote is not None:
        q = _find_message(chat, quote)
        opts["quote"] = {"localId": q["localId"], "createTime": q["createTime"],
                         "name": _sender_name(chat, q["sender"])}
        row["quote"] = q["localId"]
    return _dispatch(row, lambda ex: ex.send(chat, content, opts))


def send_image(chat, path, name="", label="", source="manual"):
    """发图片。path 必须是微信(wechat 用户)读得到的本地文件,通常来自 stage_image。"""
    chat = _check_chat(chat)
    path = os.path.realpath(path or "")
    if not os.path.isfile(path):
        raise DirectSendError("no_file", "图片不存在")
    with open(path, "rb") as f:
        head = f.read(16)
    if not _image_ext(head):
        raise DirectSendError("bad_image", "只支持 PNG / JPG / GIF / WEBP 图片")
    size = os.path.getsize(path)
    if not size or size > MAX_IMAGE_BYTES:
        raise DirectSendError("too_large", "图片为空或超过 20MB")
    row = {"t": int(time.time()), "kind": "image", "chat": chat, "name": name,
           "content": label or os.path.basename(path), "path": path, "size": size, "source": source}
    return _dispatch(row, lambda ex: ex.send_image(chat, path, size))


def send_video(chat, path, name="", label="", source="manual"):
    """发视频(MP4/MOV)。path 要求同 send_image。微信自己抽封面、转码、上传 CDN。"""
    chat = _check_chat(chat)
    path = os.path.realpath(path or "")
    if not os.path.isfile(path):
        raise DirectSendError("no_file", "视频不存在")
    with open(path, "rb") as f:
        head = f.read(16)
    if not _video_ext(head):
        raise DirectSendError("bad_video", "只支持 MP4 / MOV 视频")
    size = os.path.getsize(path)
    if not size or size > MAX_VIDEO_BYTES:
        raise DirectSendError("too_large", "视频为空或超过 25MB")
    row = {"t": int(time.time()), "kind": "video", "chat": chat, "name": name,
           "content": label or os.path.basename(path), "path": path, "size": size, "source": source}
    return _dispatch(row, lambda ex: ex.send_video(chat, path, size))


def send_media(chat, path, name="", label="", source="manual"):
    """按文件头自动选图片或视频。"""
    try:
        with open(os.path.realpath(path or ""), "rb") as f:
            head = f.read(16)
    except OSError:
        raise DirectSendError("no_file", "文件不存在")
    kind = media_kind(head)
    fn = send_voice if kind == "audio" else send_video if kind == "video" else send_image
    return fn(chat, path, name, label, source)


def send_voice(chat, path, name="", label="", source="manual"):
    """发"语音":Linux 版微信不能发语音条,把音频转成带声波画面的视频发出去。

    path 可以是音频文件,也可以是 stage_audio 已转好的 .voice.mp4。"""
    path = os.path.realpath(path or "")
    if not os.path.isfile(path):
        raise DirectSendError("no_file", "音频不存在")
    with open(path, "rb") as f:
        head = f.read(16)
    if media_kind(head) == "audio" or _is_silk(head):
        with open(path, "rb") as f:
            path = stage_audio(f.read(), os.path.basename(path))
    return send_video(chat, path, name, label or ("语音 · " + os.path.basename(path)), source)


def _find_my_message(chat, local_id=None):
    """从解密库取要撤回的那一行;local_id 为空时取我在该会话最近发的一条。"""
    from core import db, messages
    me = config.wxid()
    table = messages.msg_table(chat)
    con = db.connect("message")
    try:
        if not db.table_exists(con, table):
            raise DirectSendError("not_found", "找不到这个会话的消息")
        id2name = messages._name2id_map(con)
        cols = set(db.columns(con, table))
        sel = ["local_id", "server_id", "local_type", "sort_seq", "real_sender_id", "create_time",
               "status", "message_content", "source"]
        sel += [c for c in ("WCDB_CT_message_content", "WCDB_CT_source") if c in cols]
        q = f"SELECT {', '.join(sel)} FROM {table}"
        if local_id is not None:
            rows = con.execute(q + " WHERE local_id = ?", (int(local_id),)).fetchall()
        else:
            my_ids = [k for k, v in id2name.items() if v == me]
            if not my_ids:
                raise DirectSendError("not_found", "这个会话里没有我发的消息")
            rows = con.execute(q + f" WHERE real_sender_id IN ({','.join('?' * len(my_ids))})"
                               " AND local_type != 10000 ORDER BY sort_seq DESC LIMIT 1", my_ids).fetchall()
    finally:
        con.close()
    if not rows:
        raise DirectSendError("not_found", "没找到这条消息(库可能还没同步,稍等再试)")
    r = dict(zip(sel, rows[0]))
    sender = id2name.get(r["real_sender_id"])
    if r["local_type"] == 10000:
        raise DirectSendError("already_revoked", "这条消息已经撤回了")
    if sender != me:
        raise DirectSendError("not_mine", "只能撤回自己发的消息")
    if r["status"] not in (1, 2):   # 1=发送中(解密库同步慢,实际状态以微信内存为准),2=成功
        raise DirectSendError("not_sent_ok", f"消息状态 {r['status']},不是发送成功的消息")
    content = messages._decompress(r["message_content"], r.get("WCDB_CT_message_content"))
    source = messages._decompress(r["source"], r.get("WCDB_CT_source"))
    return {
        "type": int(r["local_type"]), "sender": me, "chat": chat, "localId": int(r["local_id"]),
        "serverId": str(int(r["server_id"]) & 0xFFFFFFFFFFFFFFFF), "sortSeq": str(int(r["sort_seq"])),
        "createTime": int(r["create_time"]), "status": int(r["status"]),
        "content": content if r["local_type"] == 1 else "", "source": source,
    }


def revoke(chat, local_id=None, name="", source="manual"):
    """撤回我发的消息。local_id 为空=该会话我最近发的一条。成功返回结果字典;失败抛 DirectSendError。"""
    chat = _check_chat(chat)
    if chat == "filehelper":
        raise DirectSendError("bad_chat", "文件传输助手不支持撤回")
    msg = _find_my_message(chat, local_id)
    age = int(time.time()) - msg["createTime"]
    if age > REVOKE_WINDOW:
        raise DirectSendError("too_old", f"消息已发出 {age} 秒,超过 2 分钟不能撤回")
    row = {"t": int(time.time()), "kind": "revoke", "chat": chat, "name": name,
           "content": msg["content"][:100] or f"[类型{msg['type']}]", "local_id": msg["localId"],
           "source": source}
    return _dispatch(row, lambda ex: ex.revoke(msg), wait=20.0)


_FNAME_DROP = re.compile(r"[\s\\/:*?\"<>|，。！？、；：,.!?;~…—'“”‘’()（）\[\]【】]+")


def _voice_filename(text):
    """mp3 文件名:语音_正文开头(去掉标点和文件名非法字符)。"""
    head = _FNAME_DROP.sub("", str(text or ""))[:12]
    return f"语音_{head or time.strftime('%H%M%S')}.mp3"


def send_tts(chat, text, voice=None, name="", source="manual", send_as=None):
    """文字 → 配置的音色合成 mp3 → 按配置发成声波视频或 mp3 文件。voice=音色 key,空=默认。"""
    from core import tts
    chat = _check_chat(chat)
    mode = send_as or tts.send_as()
    if mode not in tts.SEND_AS:
        raise DirectSendError("bad_param", "send_as 只能是 video、file 或 voice_bar")
    if mode == "voice_bar":
        return _send_voice_bar(chat, text, voice, name, source)
    try:
        mp3 = tts.synth(text, voice)
        label = "语音(" + tts.get_voice(voice)["label"] + ") · " + " ".join(str(text).split())[:60]
    except tts.TTSError as e:
        raise DirectSendError(e.code if e.code in ("empty", "too_long") else "tts_failed", e.message)
    if mode == "file":
        with open(mp3, "rb") as f:
            path = stage_file(f.read(), _voice_filename(text))
        return send_file(chat, path, name, label, source)
    return send_voice(chat, mp3, name, label, source)


MAX_FILE_BYTES = 100 * 1024 * 1024
FILE_DIR = os.path.join(config.WORK_DIR, "direct_files")


def stage_file(data, filename):
    """按原文件名存一份(对方看到的就是这个名字),返回路径。"""
    if not data:
        raise DirectSendError("empty", "文件是空的")
    if len(data) > MAX_FILE_BYTES:
        raise DirectSendError("too_large", "文件超过 100MB")
    name = os.path.basename(str(filename or "")).strip().replace("\x00", "") or "file.bin"
    d = os.path.join(FILE_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name[:120])
    with open(path, "wb") as f:
        f.write(data)
    return path


def send_file(chat, path, name="", label="", source="manual"):
    """发文件(对方看到文件卡片,显示的是文件名)。mp3 等音频这样发,点开用微信自带预览播放。"""
    chat = _check_chat(chat)
    path = os.path.realpath(path or "")
    if not os.path.isfile(path):
        raise DirectSendError("no_file", "文件不存在")
    size = os.path.getsize(path)
    if not size or size > MAX_FILE_BYTES:
        raise DirectSendError("too_large", "文件为空或超过 100MB")
    row = {"t": int(time.time()), "kind": "file", "chat": chat, "name": name,
           "content": label or os.path.basename(path), "path": path, "size": size, "source": source}
    return _dispatch(row, lambda ex: ex.send_file(chat, path, size))


def _send_voice_bar(chat, text, voice, name, source):
    """真语音条走界面录音(core/voice_bar.py),不经过 Frida;结果也记进直发历史。"""
    from core import tts, voice_bar
    row = {"t": int(time.time()), "kind": "voice_bar", "chat": chat, "name": name, "source": source}
    try:
        row["content"] = "语音条(" + tts.get_voice(voice)["label"] + ") · " + " ".join(str(text).split())[:60]
        res = voice_bar.send_tts(chat, text, voice, name)
    except (voice_bar.VoiceBarError, tts.TTSError) as e:
        row.update(ok=False, code=e.code, error=e.message)
        _record(row)
        raise DirectSendError(e.code if e.code in NOT_SENT_CODES else "voice_bar_failed", e.message)
    ok = res.get("status") in ("submitted", "confirmed")
    row.update(ok=ok, status=res.get("status"), seconds=res.get("seconds"),
               message="已发出语音条" if ok else "",
               **({} if ok else {"code": res.get("reason") or "voice_bar_failed",
                                 "error": f"语音条没发出:{res.get('reason')}"}))
    _record(row)
    if not ok:
        raise DirectSendError(row["code"], row["error"])
    return row


VOICE_MAX_MS = 59000


def encode_silk(audio_path):
    """任意音频 → 微信格式 SILK(\\x02#!SILK_V3,24kHz)。返回 (silk 路径, 时长毫秒)。

    pilk 是 C 扩展,容器里由镜像安装;没有就报 no_silk_encoder,调用方改走界面录音。"""
    try:
        import pilk
    except ImportError:
        raise DirectSendError("no_silk_encoder", "没有 pilk(SILK 编码器)")
    import struct
    import subprocess
    os.makedirs(MEDIA_DIR, exist_ok=True)
    base = os.path.join(MEDIA_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}")
    pcm, silk = base + ".pcm", base + ".silk"
    r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", audio_path,
                        "-t", str(VOICE_MAX_MS / 1000), "-ac", "1", "-ar", "24000", "-f", "s16le", pcm],
                       capture_output=True, timeout=120)
    if r.returncode != 0 or not os.path.getsize(pcm):
        raise DirectSendError("encode_failed", "音频转 PCM 失败")
    try:
        pilk.encode(pcm, silk, pcm_rate=24000, tencent=True)
    except Exception as e:  # noqa: BLE001
        raise DirectSendError("encode_failed", f"SILK 编码失败:{e}")
    finally:
        try:
            os.remove(pcm)
        except OSError:
            pass
    with open(silk, "rb") as f:
        d = f.read()
    if not d.startswith(b"\x02#!SILK_V3"):
        raise DirectSendError("encode_failed", "SILK 文件头不对")
    i, frames = 10, 0
    while i + 2 <= len(d):
        n = struct.unpack_from("<h", d, i)[0]
        if n <= 0 or i + 2 + n > len(d):
            break
        i += 2 + n
        frames += 1
    ms = frames * 20                       # SILK 每帧 20 毫秒
    if ms < 1000:
        raise DirectSendError("encode_failed", "语音太短(不到 1 秒)")
    return silk, ms


def send_voice_msg(chat, audio_path, name="", label="", source="manual"):
    """直发语音条(微信 4.1.9+):本地编码 SILK,直接构造语音请求交给微信,不经过界面录音。"""
    chat = _check_chat(chat)
    if not os.path.isfile(audio_path or ""):
        raise DirectSendError("no_file", "音频不存在")
    silk, ms = encode_silk(audio_path)
    row = {"t": int(time.time()), "kind": "voice_bar", "chat": chat, "name": name, "source": source,
           "content": label or "语音条", "seconds": round(ms / 1000, 1), "via": "direct"}
    return _dispatch(row, lambda ex: ex.send_voice(chat, silk, ms))


def voice_supported():
    """当前微信版本能不能直发语音条(不附加,只看配置)。"""
    sha, _ver = binary_version()
    if not (sha and PROFILES.get(sha, {}).get("voice")):
        return False
    try:
        import pilk  # noqa: F401  没有编码器就交给界面录音
    except ImportError:
        return False
    return True


def _profile():
    sha, _ver = binary_version()
    return PROFILES.get(sha) if sha else None


def at_supported():
    """当前微信版本的直发能不能带 @ 提醒(不附加,只看配置)。"""
    p = _profile()
    return bool(p and p.get("fields", {}).get("atList"))


def quote_supported():
    """当前微信版本能不能直发引用回复(不附加,只看配置)。"""
    p = _profile()
    return bool(p and p.get("quote") and (p.get("revoke") or {}).get("style") == "batch")
