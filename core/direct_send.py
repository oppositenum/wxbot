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

STEP_TEXT = {
    1: "取服务对象失败(第1步)", 2: "取服务对象失败(第2步)", 3: "取服务对象失败(第3步)",
    4: "服务对象类型不符,可能版本不对", 9: "已交给微信发送",
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


def send(chat, content, name=""):
    """chat=wxid 或 xxx@chatroom。返回结果字典;失败抛 DirectSendError。"""
    chat = (chat or "").strip()
    content = content if isinstance(content, str) else ""
    if not chat or any(c.isspace() for c in chat):
        raise DirectSendError("bad_chat", "请选择发送对象")
    if not content.strip():
        raise DirectSendError("empty", "内容不能为空")
    if len(content) > MAX_CHARS:
        raise DirectSendError("too_long", f"内容超过 {MAX_CHARS} 字")
    row = {"t": int(time.time()), "chat": chat, "name": name, "content": content}
    with _lock:
        try:
            script = _ensure_script()
            r = script.exports_sync.send(chat, content)
            if not r.get("ok"):
                code = r.get("error") or "failed"
                msg = {"not_logged_in": "微信还没登录完成(停在登录页?)",
                       "request_mismatch": "请求对象校验失败,未发送"}.get(code, code)
                raise DirectSendError(code, msg)
            deadline = time.time() + WAIT_SECONDS
            step = 0
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
    if step == 9:
        row.update(ok=True, step=step, message=STEP_TEXT[9])
    elif step:
        row.update(ok=False, step=step, code="step_failed", error=STEP_TEXT.get(step, f"step={step}"))
    else:
        row.update(ok=False, step=0, code="timeout", error="等待超时,协程没有执行(微信可能卡住)")
    _record(row)
    if not row["ok"]:
        raise DirectSendError(row["code"], row["error"])
    return row
