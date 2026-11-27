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
import sys
import threading
import time

import config

WECHAT_BIN = os.environ.get("WXBOT_WECHAT_BIN", "/opt/wechat/wechat")
SUPPORTED_SHA256 = {
    "e40116597803a5e0d9fc4a2b727df4b8f7a3dd6de1c336140ea373be2b6626e9": "4.1.8 aarch64",
}
FRIDA_PATHS = [p for p in (os.environ.get("WXBOT_FRIDA_PATH"), "/tmp/fr",
                           os.path.join(config.WORK_DIR, "frida")) if p]
AGENT_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "direct_send_agent.js")
HISTORY = os.path.join(config.WORK_DIR, "direct_send_history.jsonl")
MAX_CHARS = 2000
WAIT_SECONDS = 8.0

# 这些错误码出现时,消息一定没有交给微信(可安全改走其它方式);其余(timeout/frida_error)结果不确定。
NOT_SENT_CODES = frozenset({
    "no_frida", "no_wechat", "unsupported", "not_running", "attach_failed", "agent_error",
    "not_logged_in", "request_mismatch", "bad_chat", "empty", "too_long", "no_file",
    "bad_image", "bad_video", "too_large", "step_failed",
    "not_found", "not_mine", "not_sent_ok", "too_old", "already_revoked",
})

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
        source = f.read()
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


def media_kind(head):
    """按文件头判断 'image' / 'video' / None。"""
    return "image" if _image_ext(head) else "video" if _video_ext(head) else None


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


def stage_media(data, filename=""):
    """图片或视频都行,返回 (kind, path)。"""
    if media_kind(data[:16]) == "video":
        return "video", stage_video(data, filename)
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
                       "request_mismatch": "请求对象校验失败,未发送"}.get(code, code)
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
        ok = bool(p.get("result"))
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


def send(chat, content, name="", source="manual"):
    """发文字。chat=wxid 或 xxx@chatroom。返回结果字典;失败抛 DirectSendError。"""
    chat = _check_chat(chat)
    content = content if isinstance(content, str) else ""
    if not content.strip():
        raise DirectSendError("empty", "内容不能为空")
    if len(content) > MAX_CHARS:
        raise DirectSendError("too_long", f"内容超过 {MAX_CHARS} 字")
    row = {"t": int(time.time()), "kind": "text", "chat": chat, "name": name, "content": content,
           "source": source}
    return _dispatch(row, lambda ex: ex.send(chat, content))


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
    fn = send_video if media_kind(head) == "video" else send_image
    return fn(chat, path, name, label, source)


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
