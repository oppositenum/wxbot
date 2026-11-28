"""wxbot 控制台:一个入口管理多个微信。

每个微信仍是一个独立容器(自己的微信、数据卷、Flask 后台 :5100、noVNC :6080),互不串号。
控制台是另一个容器,浏览器只连它(默认 :5090):
  - 账号注册表:自动纳入已有的 wxbot* 容器;「添加微信」按模板创建新容器(Docker Engine API,经挂载的 docker.sock)
  - 状态汇总:容器是否在跑、微信是否在跑/已登录、密钥、直发、机器人
  - 反向代理:/a/<账号>/... → 该容器的后台;/a/<账号>/websockify → 该容器的 noVNC(扫码/看桌面)
    控制台用同一套后台账号密码替你登录各容器,浏览器不再需要分别登录、也不用记端口
  - 旧页面(还没重做的)经代理嵌进新界面:注入脚本把它们的 /api 请求改到对应账号,注入样式统一外观

只用标准库 + aiohttp。配置全部来自环境变量(见 README「控制台」)。
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
NOVNC = Path(os.environ.get("CONSOLE_NOVNC_DIR", "/usr/share/novnc"))
DATA = Path(os.environ.get("CONSOLE_DATA", "/data"))
REGISTRY = DATA / "accounts.json"
PORT = int(os.environ.get("CONSOLE_PORT", "5090"))
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
NETWORK = os.environ.get("CONSOLE_NETWORK", "wxbot-net")
NAME_RE = re.compile(r"^wxbot(-[a-z0-9][a-z0-9-]*)?$")
ACCOUNT_PREFIX = os.environ.get("CONSOLE_ACCOUNT_PREFIX", "wxbot-acc")
UI_USER = os.environ.get("WXBOT_UI_USER", "admin")
UI_PASSWORD = os.environ.get("WXBOT_UI_PASSWORD", "")
SECRET = (os.environ.get("WXBOT_SECRET") or secrets.token_hex(16)).encode()
SESSION_HOURS = int(os.environ.get("CONSOLE_SESSION_HOURS", "24"))
COOKIE = "wxconsole"
STATUS_TTL = 3.0
INTERNAL_WEB, INTERNAL_VNC = 5100, 6080


# ---------------- 登录 ----------------

def _sign(payload):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    mac = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + mac


def _verify(token):
    try:
        raw_b64, mac = token.rsplit(".", 1)
        raw = base64.urlsafe_b64decode(raw_b64.encode() + b"=" * (-len(raw_b64) % 4))
        if not hmac.compare_digest(hmac.new(SECRET, raw, hashlib.sha256).hexdigest(), mac):
            return None
        d = json.loads(raw)
        return d if d.get("exp", 0) > time.time() else None
    except Exception:  # noqa: BLE001 任何解析失败都当未登录
        return None


# 登录防爆破:
# 1) 登录校验全站串行,失败等 1 秒 → 全世界加起来每秒最多试 1 次密码(并发也绕不过)
# 2) 反代传了真实 IP(X-Real-IP / X-Forwarded-For)时,同一 IP 10 分钟错 5 次锁 15 分钟。
#    没传真实 IP 时所有人看起来是同一个来源(隧道),这时不按 IP 锁,免得别人故意输错把你也锁在外面。
FAIL_WINDOW, FAIL_PER_IP, LOCK_IP, FAIL_SLEEP = 600, 5, 900, 1.0
_fails = {}            # ip -> [时间…]
_locked = {}           # ip -> 解锁时间
_login_lock = None


def client_ip(request):
    """反代给的真实 IP;没有就返回 None(不按 IP 锁)。"""
    xff = request.headers.get("X-Forwarded-For", "")
    return request.headers.get("X-Real-IP") or (xff.split(",")[-1].strip() if xff else None)


def _login_locked(ip):
    return (_locked.get(ip, 0) - time.time()) if ip else 0


def _record_fail(ip):
    if not ip:
        return
    now = time.time()
    lst = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW] + [now]
    _fails[ip] = lst
    if len(lst) >= FAIL_PER_IP:
        _locked[ip] = now + LOCK_IP
    if len(_fails) > 5000:                                  # 防止被随机 IP 撑爆内存
        for k in [k for k, v in _fails.items() if now - v[-1] > FAIL_WINDOW]:
            _fails.pop(k, None)


def is_https(request):
    return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


def logged_in(request):
    return bool(_verify(request.cookies.get(COOKIE, "")))


PUBLIC = ("/ui/login.html", "/ui/app.css", "/ui/login.js", "/api/console/login", "/api/console/auth", "/favicon.ico")


@web.middleware
async def security_headers(request, handler):
    resp = await handler(request)
    if not resp.prepared:
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")         # 别的网站不能把控制台套进 iframe 骗点击
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


@web.middleware
async def auth_mw(request, handler):
    p = request.path
    if p in PUBLIC or p.startswith("/ui/fonts/") or logged_in(request):
        return await handler(request)
    if p.startswith("/api/") or p.startswith("/a/") and "/api/" in p:
        return web.json_response({"error": "需要登录"}, status=401)
    raise web.HTTPFound("/ui/login.html")


async def api_login(request):
    d = await request.json()
    if not UI_PASSWORD:
        return web.json_response({"ok": False, "error": "没有配置 WXBOT_UI_PASSWORD"}, status=503)
    global _login_lock
    ip = client_ip(request)
    wait = _login_locked(ip)
    if wait > 0:
        return web.json_response({"ok": False, "error": f"密码错误次数太多,{int(wait // 60) + 1} 分钟后再试"}, status=429)
    _login_lock = _login_lock or asyncio.Lock()
    async with _login_lock:
        ok = (hmac.compare_digest(str(d.get("username", "")).encode(), UI_USER.encode())      # bytes:中文密码不会 500
              & hmac.compare_digest(str(d.get("password", "")).encode(), UI_PASSWORD.encode()))
        if not ok:
            _record_fail(ip)
            print(f"[console] 登录失败 ip={ip or request.remote}", flush=True)
            await asyncio.sleep(FAIL_SLEEP)
            return web.json_response({"ok": False, "error": "账号或密码不对"}, status=401)
    _fails.pop(ip, None)
    resp = web.json_response({"ok": True})
    resp.set_cookie(COOKIE, _sign({"u": UI_USER, "exp": time.time() + SESSION_HOURS * 3600}),
                    httponly=True, samesite="Lax", secure=is_https(request), max_age=SESSION_HOURS * 3600)
    return resp


async def api_logout(request):
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE)
    return resp


async def api_auth(request):
    ok = logged_in(request)
    return web.json_response({"ok": ok, "user": UI_USER if ok else None})   # 没登录不暴露用户名


# ---------------- Docker ----------------

class Docker:
    """Docker Engine API(经 unix socket)。只做控制台需要的几件事。"""

    def __init__(self, sock=DOCKER_SOCK):
        self.sock = sock
        self._s = None

    def session(self):
        if self._s is None or self._s.closed:
            self._s = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=self.sock),
                                            timeout=aiohttp.ClientTimeout(total=60))
        return self._s

    async def req(self, method, path, body=None, params=None, ok=(200, 201, 204, 304)):
        async with self.session().request(method, "http://docker" + path, json=body, params=params) as r:
            text = await r.text()
            data = json.loads(text) if text and text[:1] in "[{" else text
            if r.status not in ok:
                msg = data.get("message") if isinstance(data, dict) else str(data)[:200]
                raise DockerError(r.status, msg or f"HTTP {r.status}")
            return data

    async def containers(self):
        return await self.req("GET", "/containers/json", params={"all": "1"})

    async def inspect(self, name):
        return await self.req("GET", f"/containers/{name}/json")

    async def action(self, name, op):
        return await self.req("POST", f"/containers/{name}/{op}")

    async def ensure_network(self):
        try:
            await self.req("GET", f"/networks/{NETWORK}")
        except DockerError as e:
            if e.status != 404:
                raise
            await self.req("POST", "/networks/create", {"Name": NETWORK, "Driver": "bridge"})

    async def connect(self, name):
        try:
            await self.req("POST", f"/networks/{NETWORK}/connect", {"Container": name})
        except DockerError as e:
            if "already exists" not in e.message and e.status != 403:
                raise

    async def exec(self, name, cmd, user="wechat"):
        ex = await self.req("POST", f"/containers/{name}/exec",
                            {"Cmd": cmd, "User": user, "AttachStdout": False, "AttachStderr": False})
        await self.req("POST", f"/exec/{ex['Id']}/start", {"Detach": True})


class DockerError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


docker = Docker()


# ---------------- 注册表 ----------------

def load_registry():
    try:
        d = json.loads(REGISTRY.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and isinstance(d.get("accounts"), list) else {"accounts": []}
    except (OSError, ValueError):
        return {"accounts": []}


def save_registry(d):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(REGISTRY)


def find(acct_id):
    for a in load_registry()["accounts"]:
        if a["id"] == acct_id:
            return a
    return None


SELF_ID = os.environ.get("HOSTNAME", "")


async def self_container():
    """控制台自己的容器名(不把自己当成微信账号)。"""
    try:
        return (await docker.inspect(SELF_ID))["Name"].lstrip("/")
    except Exception:  # noqa: BLE001
        return ""


async def adopt(include_ignored=False):
    """把已有的 wxbot* 微信容器纳入注册表(不重建、不改动),并接入控制台网络。返回新纳入的数量。
    手动从控制台移除的旧容器记在 ignored 里,启动时不再自动纳入;手动「扫描」(include_ignored)才会。"""
    me = await self_container()
    reg = load_registry()
    if include_ignored:
        reg["ignored"] = []
    known = {a["container"] for a in reg["accounts"]} | set(reg.get("ignored") or [])
    added = 0
    for c in await docker.containers():
        name = (c.get("Names") or ["/"])[0].lstrip("/")
        if name == me or not NAME_RE.match(name) or name in known or name.endswith("-old") or "-old-" in name:
            continue
        info = await docker.inspect(name)
        exposed = (info.get("Config") or {}).get("ExposedPorts") or {}
        labels = (info.get("Config") or {}).get("Labels") or {}
        if f"{INTERNAL_WEB}/tcp" not in exposed and labels.get("com.oppositenum.wxbot.account") is None:
            continue
        reg["accounts"].append({"id": _new_id(reg), "label": labels.get("com.oppositenum.wxbot.label") or "",
                                "container": name, "managed": bool(labels.get("com.oppositenum.wxbot.account")),
                                "created": int(time.time())})
        known.add(name)
        added += 1
    if added or include_ignored:
        save_registry(reg)
    for a in reg["accounts"]:
        try:
            await docker.connect(a["container"])
        except Exception:  # noqa: BLE001 容器没了/没在跑:状态里会体现
            pass
    return added


def _new_id(reg):
    used = {a["id"] for a in reg["accounts"]}
    n = 1
    while f"w{n}" in used:
        n += 1
    return f"w{n}"


# ---------------- 各容器后台的会话(控制台替浏览器登录) ----------------

class Backend:
    def __init__(self, container):
        self.container = container
        self.base = f"http://{container}:{INTERNAL_WEB}"
        self.s = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True),
                                       timeout=aiohttp.ClientTimeout(total=120, sock_connect=3))
        self.lock = asyncio.Lock()

    async def login(self):
        async with self.s.post(self.base + "/api/auth/login",
                               json={"username": UI_USER, "password": UI_PASSWORD}) as r:
            return r.status == 200

    async def request(self, method, path, **kw):
        """带登录态的请求;401 时登录一次再重试。返回 aiohttp 响应(调用方负责 release)。"""
        r = await self.s.request(method, self.base + path, allow_redirects=False, **kw)
        if r.status == 401 or (r.status in (301, 302) and "/login" in r.headers.get("Location", "")):
            r.release()
            async with self.lock:
                await self.login()
            if "data" in kw and hasattr(kw["data"], "seek"):
                kw["data"].seek(0)
            r = await self.s.request(method, self.base + path, allow_redirects=False, **kw)
        return r

    async def json(self, path, timeout=4):
        r = await self.request("GET", path, timeout=aiohttp.ClientTimeout(total=timeout, sock_connect=2))
        try:
            if r.status != 200:
                return None
            return await r.json(content_type=None)
        finally:
            r.release()


_backends = {}


def backend(acct):
    b = _backends.get(acct["container"])
    if b is None:
        b = _backends[acct["container"]] = Backend(acct["container"])
    return b


# ---------------- 状态 ----------------

_status_cache = {}


async def account_status(acct, fresh=False):
    hit = _status_cache.get(acct["id"])
    if hit and not fresh and time.time() - hit[0] < STATUS_TTL:
        return hit[1]
    st = {"id": acct["id"], "label": acct["label"], "container": acct["container"], "managed": acct.get("managed"),
          "state": "unknown", "container_state": "", "backend": False}
    try:
        info = await docker.inspect(acct["container"])
        cs = info.get("State") or {}
        st["container_state"] = cs.get("Status", "")
        st["started_at"] = cs.get("StartedAt", "")
        st["image"] = (info.get("Config") or {}).get("Image", "")
    except DockerError as e:
        st["state"] = "missing" if e.status == 404 else "error"
        st["error"] = e.message
        _status_cache[acct["id"]] = (time.time(), st)
        return st
    if st["container_state"] != "running":
        st["state"] = "stopped"
        _status_cache[acct["id"]] = (time.time(), st)
        return st
    b = backend(acct)
    try:
        s, me, ds, bot, mode = await asyncio.gather(
            b.json("/api/status"), b.json("/api/me"), b.json("/api/direct/status"), b.json("/api/bot"),
            b.json("/api/send_mode"), return_exceptions=True)
    except Exception:  # noqa: BLE001
        s = me = ds = bot = mode = None
    ok = lambda x: x if isinstance(x, dict) else None
    s, me, ds, bot, mode = ok(s), ok(me), ok(ds), ok(bot), ok(mode)
    st["backend"] = s is not None
    if s is None:
        st["state"] = "starting"
    else:
        st.update(wechat_running=bool(s.get("wechat_running")), logged_in=bool(s.get("logged_in")),
                  keys_ready=bool(s.get("keys_ready")), img_ready=bool(s.get("img_ready")))
        if not s.get("wechat_running"):
            st["state"] = "wechat_down"
        elif not s.get("logged_in"):
            st["state"] = "need_login"
        elif not s.get("keys_ready"):
            st["state"] = "keys_missing"
        else:
            st["state"] = "online"
    if me:
        st.update(wxid=me.get("wxid") or "", name=me.get("name") or "", head_url=me.get("head_url") or "")
    if ds:
        st["direct"] = {k: ds.get(k) for k in ("supported", "version", "attached", "logged_in", "revoke",
                                                "voice", "quote", "at")}
    if bot:
        st["bot_running"] = bool(bot.get("running"))
    if mode:
        st["send_mode"] = mode.get("mode")
    _status_cache[acct["id"]] = (time.time(), st)
    return st


async def api_accounts(request):
    reg = load_registry()
    accts = sorted(reg["accounts"], key=lambda a: (a.get("order", 1000), a.get("created", 0)))
    sts = await asyncio.gather(*(account_status(a, fresh=request.query.get("fresh") == "1") for a in accts))
    return web.json_response({"accounts": list(sts)})


async def api_scan(request):
    try:
        await docker.ensure_network()
        n = await adopt(include_ignored=True)
    except Exception as e:  # noqa: BLE001
        return web.json_response({"ok": False, "error": f"扫描失败:{e}"}, status=500)
    return web.json_response({"ok": True, "added": n})


# ---------------- 创建/启停/删除 ----------------

def _mac():
    return "00:1b:21:" + ":".join(f"{b:02x}" for b in os.urandom(3))


async def _account_image():
    img = os.environ.get("WXBOT_ACCOUNT_IMAGE")
    if img:
        return img
    try:
        return (await docker.inspect(SELF_ID))["Config"]["Image"]
    except Exception:  # noqa: BLE001
        raise web.HTTPBadRequest(text="没有配置 WXBOT_ACCOUNT_IMAGE,也读不到控制台自己的镜像")


async def api_create(request):
    d = await request.json()
    label = str(d.get("label") or "").strip()[:30]          # 备注,可空(空时显示微信昵称)
    reg = load_registry()
    used = {a["container"] for a in reg["accounts"]}
    names = {(c.get("Names") or ["/"])[0].lstrip("/") for c in await docker.containers()}
    n = 1
    while f"{ACCOUNT_PREFIX}{n}" in used | names:
        n += 1
    name = f"{ACCOUNT_PREFIX}{n}"
    acct_id = _new_id(reg)
    image = await _account_image()
    env = {
        "WXBOT_UI_AUTH": "1", "WXBOT_UI_USER": UI_USER, "WXBOT_UI_PASSWORD": UI_PASSWORD,
        "WXBOT_SECRET": secrets.token_hex(16), "VNC_PASSWORD": os.environ.get("VNC_PASSWORD", secrets.token_hex(8)),
        "WXBOT_SELF_CONTAINER": name, "WXBOT_BOT_AUTOSTART": "1", "WXBOT_SEND_MODE": "direct",
        "WXBOT_MEDIA_CAPTURE": os.environ.get("WXBOT_MEDIA_CAPTURE", "1"),
        "TZ": os.environ.get("TZ", "Asia/Shanghai"), "SCREEN": os.environ.get("SCREEN", "1920x1080x24"),
    }
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    body = {
        "Image": image, "Hostname": f"ubuntu-pc-{n}",
        "Env": [f"{k}={v}" for k, v in env.items()],
        "Labels": {"com.oppositenum.wxbot.account": acct_id, "com.oppositenum.wxbot.label": label},
        "HostConfig": {
            "Init": True, "ShmSize": 1 << 30, "SecurityOpt": ["seccomp=unconfined"], "CapAdd": ["SYS_PTRACE"],
            "RestartPolicy": {"Name": "unless-stopped"}, "NetworkMode": NETWORK,
            "Mounts": [{"Type": "volume", "Source": f"{name}-home", "Target": "/home/wechat"},
                       {"Type": "volume", "Source": f"{name}-data", "Target": "/app/accounts"}],
        },
        "NetworkingConfig": {"EndpointsConfig": {NETWORK: {"MacAddress": _mac(), "Aliases": [name]}}},
    }
    try:
        await docker.ensure_network()
        await docker.req("POST", "/containers/create", body, params={"name": name})
        await docker.action(name, "start")
    except DockerError as e:
        return web.json_response({"ok": False, "error": f"创建容器失败:{e.message}"}, status=500)
    reg = load_registry()
    reg["accounts"].append({"id": acct_id, "label": label, "container": name, "managed": True,
                            "created": int(time.time())})
    save_registry(reg)
    return web.json_response({"ok": True, "id": acct_id, "container": name})


async def api_action(request):
    acct = find(request.match_info["id"])
    if not acct:
        return web.json_response({"ok": False, "error": "没有这个账号"}, status=404)
    op = (await request.json()).get("op")
    try:
        if op in ("start", "stop", "restart"):
            await docker.action(acct["container"], op)
        elif op == "restart_wechat":
            await docker.exec(acct["container"], ["bash", "-lc",
                              "pkill -x wechat; for i in $(seq 1 10); do pgrep -x wechat >/dev/null || break; sleep 1; done; "
                              "export DISPLAY=:0 XDG_RUNTIME_DIR=/tmp/runtime-$(id -u); cd ~; "
                              "nohup wechat >/dev/null 2>&1 &"])
        elif op == "enter_wechat":
            # 4.1.13 重启后的「进入微信」按钮(1920x1080 屏幕上登录框居中)
            await docker.exec(acct["container"], ["bash", "-lc", "DISPLAY=:0 xdotool mousemove 959 636 click 1"])
        else:
            return web.json_response({"ok": False, "error": "不支持的操作"}, status=400)
    except DockerError as e:
        return web.json_response({"ok": False, "error": e.message}, status=500)
    _status_cache.pop(acct["id"], None)
    return web.json_response({"ok": True})


async def api_update(request):
    acct_id = request.match_info["id"]
    d = await request.json()
    reg = load_registry()
    for a in reg["accounts"]:
        if a["id"] == acct_id:
            if "label" in d:                                   # 备注;传空字符串 = 清掉,改显示微信昵称
                a["label"] = str(d["label"] or "").strip()[:30]
            if isinstance(d.get("order"), int):
                a["order"] = d["order"]
            save_registry(reg)
            _status_cache.pop(acct_id, None)
            return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "没有这个账号"}, status=404)


async def api_delete(request):
    """移出控制台。控制台创建的容器同时删除容器;purge=1 再删数据卷(微信登录、聊天记录、密钥全部清掉)。
    纳入进来的旧容器只移出列表,不动容器本身。"""
    acct = find(request.match_info["id"])
    if not acct:
        return web.json_response({"ok": False, "error": "没有这个账号"}, status=404)
    purge = request.query.get("purge") == "1"
    if acct.get("managed"):
        try:
            await docker.req("DELETE", f"/containers/{acct['container']}", params={"force": "1"}, ok=(204, 404))
            if purge:
                for v in (f"{acct['container']}-home", f"{acct['container']}-data"):
                    await docker.req("DELETE", f"/volumes/{v}", ok=(204, 404))
        except DockerError as e:
            return web.json_response({"ok": False, "error": e.message}, status=500)
    reg = load_registry()
    reg["accounts"] = [a for a in reg["accounts"] if a["id"] != acct["id"]]
    if not acct.get("managed"):
        reg["ignored"] = sorted(set(reg.get("ignored") or []) | {acct["container"]})
    save_registry(reg)
    b = _backends.pop(acct["container"], None)
    if b:
        await b.s.close()
    return web.json_response({"ok": True})


# ---------------- 反向代理 ----------------

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "cookie", "content-length", "content-encoding", "set-cookie"}

SHIM = """<script>(function(){var P=%s;
function fix(u){return (typeof u==='string'&&u.charAt(0)==='/'&&u.charAt(1)!=='/'&&u.indexOf(P+'/')!==0)?P+u:u;}
var F=window.fetch;window.fetch=function(u,o){if(u&&u.url&&!(typeof u==='string'))return F.call(this,u,o);return F.call(this,fix(u),o);};
var O=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){arguments[1]=fix(u);return O.apply(this,arguments);};
var A=window.open;window.open=function(u){arguments[0]=fix(u);return A.apply(this,arguments);};
try{var R=location.replace.bind(location);location.replace=function(u){return R(fix(u));};}catch(e){}
window.__wxconsole=P;})();</script>
<link rel="stylesheet" href="/ui/legacy.css">"""
ATTR_RE = re.compile(r'''((?:src|href|action)=["'])/(?!/|a/)''')


def _inject(html, prefix):
    html = ATTR_RE.sub(lambda m: m.group(1) + prefix + "/", html)
    shim = SHIM % json.dumps(prefix)
    m = re.search(r"<head[^>]*>", html, re.I)
    return html[:m.end()] + shim + html[m.end():] if m else shim + html


async def proxy(request, acct=None, tail=None):
    acct = acct or find(request.match_info["id"])
    if not acct:
        return web.json_response({"error": "没有这个账号"}, status=404)
    tail = tail if tail is not None else request.match_info.get("tail", "")
    if tail == "websockify":
        return await ws_proxy(request, acct)
    path = "/" + tail + (("?" + request.query_string) if request.query_string else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP and k.lower() != "referer"}
    body = await request.read() if request.can_read_body else None
    b = backend(acct)
    try:
        r = await b.request(request.method, path, headers=headers, data=body)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return web.json_response({"error": f"连不上这个微信的后台({acct['label'] or acct['container']}):{e.__class__.__name__}"},
                                 status=502)
    try:
        out_headers = {k: v for k, v in r.headers.items() if k.lower() not in HOP}
        if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location", "").startswith("/"):
            out_headers["Location"] = f"/a/{acct['id']}" + r.headers["Location"]
        ctype = r.headers.get("Content-Type", "")
        if "text/html" in ctype:
            html = await r.text()
            resp = web.Response(status=r.status, text=_inject(html, f"/a/{acct['id']}"), content_type="text/html")
            resp.set_cookie("wxacct", acct["id"], samesite="Lax")
            return resp
        resp = web.StreamResponse(status=r.status, headers=out_headers)
        await resp.prepare(request)
        async for chunk in r.content.iter_chunked(64 * 1024):
            await resp.write(chunk)
        await resp.write_eof()
        return resp
    finally:
        r.release()


async def fallback_proxy(request):
    """旧页面里动态拼出来的绝对路径(如 <img src="/api/avatar…">)没被改写:按 Referer 或最近打开的账号转发。"""
    ref = request.headers.get("Referer", "")
    m = re.search(r"/a/([a-z0-9]+)/", ref)
    acct_id = m.group(1) if m else request.cookies.get("wxacct")
    acct = find(acct_id) if acct_id else None
    if not acct:
        return web.json_response({"error": "不知道是哪个微信的请求"}, status=404)
    return await proxy(request, acct, request.path.lstrip("/"))


async def ws_proxy(request, acct):
    """noVNC 的 WebSocket:浏览器 ↔ 控制台 ↔ 该容器 websockify。"""
    ws = web.WebSocketResponse(protocols=("binary", "base64"), max_msg_size=0)
    await ws.prepare(request)
    url = f"ws://{acct['container']}:{INTERNAL_VNC}/websockify"
    try:
        async with aiohttp.ClientSession() as s, s.ws_connect(url, protocols=("binary",), max_msg_size=0) as up:
            async def pump(src, dst):
                async for msg in src:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    else:
                        break
            await asyncio.gather(pump(ws, up), pump(up, ws), return_exceptions=True)
    except Exception:  # noqa: BLE001 容器没起/断开:关掉浏览器这端即可
        pass
    await ws.close()
    return ws


# ---------------- 静态 ----------------

async def index(request):
    return web.FileResponse(STATIC / "index.html")


async def vnc_info(request):
    """noVNC 连接参数:旧镜像的 x11vnc 无密码,新镜像用 VNC_PASSWORD。"""
    return web.json_response({"password": os.environ.get("VNC_PASSWORD", "")})


def make_app():
    app = web.Application(middlewares=[security_headers, auth_mw], client_max_size=200 * 1024 * 1024)
    app.add_routes([
        web.post("/api/console/login", api_login),
        web.post("/api/console/logout", api_logout),
        web.get("/api/console/auth", api_auth),
        web.get("/api/console/accounts", api_accounts),
        web.post("/api/console/accounts", api_create),
        web.post("/api/console/scan", api_scan),
        web.patch("/api/console/accounts/{id}", api_update),
        web.delete("/api/console/accounts/{id}", api_delete),
        web.post("/api/console/accounts/{id}/action", api_action),
        web.get("/api/console/vnc", vnc_info),
        web.get("/", index),
        web.route("*", "/a/{id}/{tail:.*}", proxy),
    ])
    app.router.add_static("/ui/", STATIC, show_index=False)
    if NOVNC.is_dir():
        app.router.add_static("/novnc/", NOVNC, show_index=False)
    app.router.add_route("*", "/{tail:(api|static)/.*}", fallback_proxy)

    async def on_start(app):
        try:
            await docker.ensure_network()
            n = await adopt()
            print(f"[console] 纳入已有微信容器 {n} 个", flush=True)
        except Exception as e:  # noqa: BLE001 没挂 docker.sock 时只能用注册表里已有的
            print(f"[console] 扫描容器失败:{e}", flush=True)

    async def on_stop(app):
        for b in _backends.values():
            await b.s.close()
        s = getattr(docker, "_s", None)
        if s and not s.closed:
            await s.close()
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host=os.environ.get("CONSOLE_BIND", "0.0.0.0"), port=PORT, access_log=None)
