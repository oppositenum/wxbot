"""Webhook 订阅:把机器人收到的消息加密推给第三方地址。

每个订阅有自己的密钥(32 字节,64 位十六进制),消息体用 AES-256-GCM 加密:

    POST <url>
    Content-Type: application/json
    X-Wxbot-Event: message | test
    X-Wxbot-Id: evt_…            (同一条消息重试时不变,接收方按它去重)
    X-Wxbot-Timestamp: 1732800000

    {"v": 1, "alg": "AES-256-GCM", "id": "evt_…", "ts": 1732800000,
     "iv": "<base64 12 字节>", "data": "<base64 密文 + 16 字节 tag>"}

解密:key = bytes.fromhex(secret);AAD = f"{id}.{ts}";明文是 JSON(见 docs/webhook.md)。
GCM 自带认证:只有拿着密钥的人能造出能解开的包,改动 id/ts/密文都会解密失败。

投递不经过微信发送锁(慢的接收方不会拖住自动回复),失败按 RETRY_DELAYS 重试,2xx 才算成功。
配置存在账号目录的 webhooks.json(含密钥,不进配置导出)。
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import config

FILE = "webhooks.json"
VERSION = 1
TIMEOUT = 8
RETRY_DELAYS = (0, 10, 60)          # 第 1 次立即,失败后 10 秒、60 秒再试
MAX_BODY = 256 * 1024               # 单条明文上限,超了截断 content
LOG_KEEP = 50
USER_AGENT = "wxbot-webhook/1"

_lock = threading.RLock()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webhook")
_logs = {}                          # hook id -> deque[投递记录]
_stats = {}                         # hook id -> {ok, fail, last_ok, last_fail, last_error}


# ---------------- 加密 ----------------

def new_secret():
    return secrets.token_hex(32)


def _aesgcm_encrypt(key, iv, plain, aad):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).encrypt(iv, plain, aad)
    except ImportError:
        from Crypto.Cipher import AES
        c = AES.new(key, AES.MODE_GCM, nonce=iv)
        c.update(aad)
        ct, tag = c.encrypt_and_digest(plain)
        return ct + tag


def _aesgcm_decrypt(key, iv, data, aad):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).decrypt(iv, data, aad)
    except ImportError:
        from Crypto.Cipher import AES
        c = AES.new(key, AES.MODE_GCM, nonce=iv)
        c.update(aad)
        return c.decrypt_and_verify(data[:-16], data[-16:])


def encrypt(secret, event_id, ts, obj):
    key = bytes.fromhex(secret)
    iv = secrets.token_bytes(12)
    plain = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    data = _aesgcm_encrypt(key, iv, plain, f"{event_id}.{ts}".encode())
    return {"v": VERSION, "alg": "AES-256-GCM", "id": event_id, "ts": ts,
            "iv": base64.b64encode(iv).decode(), "data": base64.b64encode(data).decode()}


def decrypt(secret, envelope, max_age=None):
    """参考实现(测试、文档用)。max_age 秒:超时的包当重放丢弃。"""
    if envelope.get("v") != VERSION or envelope.get("alg") != "AES-256-GCM":
        raise ValueError("unsupported envelope")
    ts = int(envelope["ts"])
    if max_age is not None and abs(time.time() - ts) > max_age:
        raise ValueError("expired")
    plain = _aesgcm_decrypt(bytes.fromhex(secret), base64.b64decode(envelope["iv"]),
                            base64.b64decode(envelope["data"]), f"{envelope['id']}.{ts}".encode())
    return json.loads(plain)


# ---------------- 配置 ----------------

def _path():
    return os.path.join(config.account_dir(), FILE)


def load():
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and isinstance(d.get("hooks"), list) else {"hooks": []}
    except (OSError, ValueError):
        return {"hooks": []}


def _save(d):
    p = _path()
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # 含密钥:只给自己读
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _valid_url(url):
    u = urllib.parse.urlsplit(url or "")
    return u.scheme in ("http", "https") and bool(u.netloc)


def _clean(body, old=None):
    h = dict(old or {})
    if "name" in body:
        h["name"] = str(body.get("name") or "").strip()[:40]
    if "url" in body:
        url = str(body.get("url") or "").strip()
        if not _valid_url(url):
            raise ValueError("地址要以 http:// 或 https:// 开头")
        h["url"] = url
    if "enabled" in body:
        h["enabled"] = bool(body["enabled"])
    if "all_chats" in body:
        h["all_chats"] = bool(body["all_chats"])
    if "sources" in body:
        h["sources"] = [str(x) for x in dict.fromkeys(body.get("sources") or []) if x][:500]
    if "source_labels" in body and isinstance(body["source_labels"], dict):   # 只给页面显示名字用
        keep = set(h.get("sources") or [])
        h["source_labels"] = {str(k): str(v)[:60] for k, v in body["source_labels"].items() if k in keep}
    if "include_self" in body:
        h["include_self"] = bool(body["include_self"])
    if "groups" in body:
        h["groups"] = bool(body["groups"])
    if "private" in body:
        h["private"] = bool(body["private"])
    if "categories" in body:
        h["categories"] = [str(x) for x in (body.get("categories") or []) if x][:30]
    return h


def public(h):
    """给页面看的:不含密钥,只给尾 4 位方便核对。"""
    out = {k: v for k, v in h.items() if k != "secret"}
    out["secret_hint"] = "…" + h.get("secret", "")[-4:]
    out["stats"] = dict(_stats.get(h["id"], {}))
    return out


def list_hooks():
    return [public(h) for h in load()["hooks"]]


def get(hook_id):
    return next((h for h in load()["hooks"] if h["id"] == hook_id), None)


def create(body):
    if not body.get("url"):
        raise ValueError("填一下接收地址")
    with _lock:
        d = load()
        if len(d["hooks"]) >= 50:
            raise ValueError("最多 50 个订阅")
        h = {"id": "wh_" + secrets.token_hex(4), "name": "", "enabled": True, "all_chats": True, "sources": [],
             "groups": True, "private": True, "include_self": False, "categories": [], "created": int(time.time())}
        h = _clean(body, h)
        h["secret"] = new_secret()
        d["hooks"].append(h)
        _save(d)
    return h                         # 新建时把完整密钥返回给页面显示一次


def update(hook_id, body):
    with _lock:
        d = load()
        for i, h in enumerate(d["hooks"]):
            if h["id"] == hook_id:
                d["hooks"][i] = _clean(body, h)
                _save(d)
                return d["hooks"][i]
    raise KeyError(hook_id)


def rotate(hook_id):
    with _lock:
        d = load()
        for h in d["hooks"]:
            if h["id"] == hook_id:
                h["secret"] = new_secret()
                h["rotated"] = int(time.time())
                _save(d)
                return h
    raise KeyError(hook_id)


def delete(hook_id):
    with _lock:
        d = load()
        n = len(d["hooks"])
        d["hooks"] = [h for h in d["hooks"] if h["id"] != hook_id]
        if len(d["hooks"]) == n:
            raise KeyError(hook_id)
        _save(d)
    _logs.pop(hook_id, None)
    _stats.pop(hook_id, None)


def deliveries(hook_id):
    return list(reversed(_logs.get(hook_id, ())))


# ---------------- 匹配 + 投递 ----------------

def active():
    return [h for h in load()["hooks"] if h.get("enabled") and h.get("url") and h.get("secret")]


def wants(h, chat, msg):
    is_group = chat.endswith("@chatroom")
    if msg.get("is_self") and not h.get("include_self"):
        return False
    if is_group and not h.get("groups", True) or (not is_group and not h.get("private", True)):
        return False
    if not h.get("all_chats", True) and chat not in set(h.get("sources") or []):
        return False
    cats = h.get("categories") or []
    if cats and (msg.get("category") or "text") not in cats:
        return False
    return True


def sources_needed(hooks=None):
    """机器人主循环要额外轮询的会话(选定会话的订阅);None = 有订阅要全部会话。"""
    out = set()
    for h in hooks if hooks is not None else active():
        if h.get("all_chats", True):
            return None
        out |= set(h.get("sources") or [])
    return out


def event_id(account, chat, local_id, hook_id):
    return "evt_" + hashlib.sha256(f"{account}|{chat}|{local_id}|{hook_id}".encode()).hexdigest()[:24]


def _fit(payload):
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw.encode()) <= MAX_BODY:
        return payload
    m = dict(payload.get("message") or {})
    m["content"] = (m.get("content") or "")[:20000]
    m["truncated"] = True
    m.pop("meta", None)
    return {**payload, "message": m}


def _record(hook_id, entry):
    with _lock:
        _logs.setdefault(hook_id, deque(maxlen=LOG_KEEP)).append(entry)
        s = _stats.setdefault(hook_id, {"ok": 0, "fail": 0})
        if entry["ok"]:
            s["ok"] += 1
            s["last_ok"] = entry["time"]
        elif entry.get("final"):
            s["fail"] += 1
            s["last_fail"] = entry["time"]
            s["last_error"] = entry.get("error") or f"HTTP {entry.get('status')}"


# 直连,不走容器里给大模型用的 http_proxy(代理连不到内网地址,也没必要让第三方流量绕代理)
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):           # 不跟随跳转:跳到别处会把密文送到没配置过的地址
        return None


_opener.add_handler(_NoRedirect())


def _post(url, body, headers):
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with _opener.open(req, timeout=TIMEOUT) as r:
            return r.getcode(), ""
    except urllib.error.HTTPError as e:
        return e.code, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return 0, type(e).__name__ + (f": {e}" if str(e) else "")


def _deliver(h, event, evt_id, payload, log, sleep=time.sleep, retry_delays=None):
    ts = int(time.time())
    delays = RETRY_DELAYS if retry_delays is None else retry_delays
    for attempt, delay in enumerate(delays, 1):
        if delay:
            sleep(delay)
        ts = int(time.time())
        env = encrypt(h["secret"], evt_id, ts, payload)          # 每次重试新 iv/ts,id 不变
        body = json.dumps(env, separators=(",", ":")).encode()
        status, err = _post(h["url"], body, {"Content-Type": "application/json", "User-Agent": USER_AGENT,
                                             "X-Wxbot-Event": event, "X-Wxbot-Id": evt_id,
                                             "X-Wxbot-Timestamp": str(ts)})
        ok = 200 <= status < 300
        final = ok or attempt == len(delays)
        _record(h["id"], {"time": ts, "event": event, "id": evt_id, "attempt": attempt, "status": status,
                          "ok": ok, "final": final, "error": err,
                          "chat": (payload.get("message") or {}).get("chat_name") or ""})
        if ok:
            return True
        if log and final:
            log(f"  [webhook] {h.get('name') or h['url']} 投递失败({attempt} 次):{err or status}")
    return False


def dispatch(chat, msg, payload, account, log=print, hooks=None):
    """主循环调用:把一条消息交给所有想要它的订阅,立刻返回。"""
    n = 0
    for h in hooks if hooks is not None else active():
        if not wants(h, chat, msg):
            continue
        evt = event_id(account, chat, msg.get("local_id"), h["id"])
        _pool.submit(_deliver, h, "message", evt, _fit({"event": "message", **payload}), log)
        n += 1
    return n


def send_test(hook_id, account_info):
    """同步发一条测试事件(不重试),返回这次投递记录。"""
    h = get(hook_id)
    if not h:
        raise KeyError(hook_id)
    now = int(time.time())
    payload = {"event": "test", "account": account_info,
               "message": {"chat": "filehelper", "chat_name": "文件传输助手", "is_group": False,
                           "sender": "", "sender_name": "", "is_self": False, "type": 1, "category": "text",
                           "category_name": "文字", "content": "【wxbot webhook 测试】能解开这条就说明配置对了",
                           "summary": "【wxbot webhook 测试】能解开这条就说明配置对了",
                           "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), "timestamp": now,
                           "local_id": 0, "server_id": None, "meta": {}}}
    _deliver(h, "test", "evt_test_" + secrets.token_hex(6), payload, None, retry_delays=(0,))
    return deliveries(hook_id)[0]
