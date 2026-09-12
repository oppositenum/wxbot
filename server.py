"""wxbot 后台：REST API + 静态前端 + 定时增量解密。

启动： python3 server.py
访问： http://localhost:5100
"""
import json
import os
import threading
import time

from flask import Flask, jsonify, request, send_file, send_from_directory, Response
import io
import zipfile
import tempfile

import config
from core import decrypt, contacts, messages, avatars, docker_wx, sender
from core import bot as botmod
from tools import wxbot_config

app = Flask(__name__, static_folder=None)
from core.personalization_api import bp as personalization_bp
app.register_blueprint(personalization_bp)

@app.get("/personalization")
def personalization_page():
    return send_from_directory(os.path.join(config.PROJECT_DIR, "static"), "personalization.html")



from core import account_session as sessions

def _config_access():
    """Allow same-origin local UI or the configured admin token (Docker bridge requests
    arrive as 172.x and cannot pass the strict loopback check)."""
    if local_management_access():
        return True
    import hmac
    expected = os.environ.get("WXBOT_ADMIN_READ_TOKEN", "")
    supplied = request.headers.get("X-Wxbot-Admin-Token", "")
    return bool(expected and supplied) and hmac.compare_digest(expected.encode(), supplied.encode())

def local_management_access():
    """Explicit local-owner access; never trust loopback alone or proxy headers.

    The custom browser header requires same-origin access (no CORS grant). A
    strict Host check also prevents a rebound external hostname gaining access.
    Remote deployments keep the existing administrator token requirement.
    """
    if os.environ.get("WXBOT_LOCAL_ADMIN") != "1":
        return False
    # Docker published-port requests arrive from the bridge gateway (172.16/12).
    if request.remote_addr not in ("127.0.0.1", "::1") and not (request.remote_addr or "").startswith("172."):
        return False
    if request.host not in (f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}", f"[::1]:{config.PORT}"):
        return False
    if request.headers.get("X-Wxbot-Local-Admin") != "1":
        return False
    if any(k.lower() == "forwarded" or k.lower().startswith("x-forwarded-") for k in request.headers.keys()):
        return False
    if request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
        return False
    origin = request.headers.get("Origin")
    return not origin or origin == request.host_url.rstrip("/")


@app.get("/api/config/export")
def api_config_export():
    if not _config_access():
        return jsonify({"error": "配置导出仅允许本机后台操作"}), 403
    out = io.BytesIO()
    files = wxbot_config.collect_files()
    manifest = {"format": "wxbot-config-v1", "created_at": int(time.time()), "files": files,
                "excluded": ["keys.json", "llm_config.json", "decrypted/", "msg/", "mediacache/"]}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("wxbot-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for rel in files: z.write(os.path.join(config.PROJECT_DIR, rel), rel)
    out.seek(0)
    return send_file(out, mimetype="application/zip", as_attachment=True, download_name="wxbot-config-backup.zip")

@app.post("/api/config/import")
def api_config_import():
    if not _config_access():
        return jsonify({"error": "配置导入仅允许本机后台操作"}), 403
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"error": "请选择配置 zip 文件"}), 400
    fd, path = tempfile.mkstemp(suffix=".zip"); os.close(fd)
    try:
        upload.save(path); wxbot_config.import_archive(path)
    except SystemExit as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": "导入失败：" + type(exc).__name__}), 400
    finally:
        try: os.unlink(path)
        except OSError: pass
    return jsonify({"ok": True, "message": "配置已导入，请重启后台"})

@app.get("/api/admin/access")
def admin_access():
    return jsonify(local_admin=local_management_access())


@app.before_request
def authorize_private_management():
    """Separate owner access to full profiles/KB; never inferred from absent tool context."""
    if request.path not in ("/api/profiles", "/api/kb", "/api/personalization") and not request.path.startswith(("/api/profiles/", "/api/kb/", "/api/personalization/")):
        return None
    import hashlib
    import hmac
    expected = os.environ.get("WXBOT_ADMIN_READ_TOKEN", "")
    supplied = request.headers.get("X-Wxbot-Admin-Token", "")
    local = local_management_access()
    allowed = local or (bool(expected and supplied) and hmac.compare_digest(expected.encode(), supplied.encode()))
    account = hashlib.sha256(config.account_key().encode()).hexdigest()[:12]
    app.logger.warning("private_management_access endpoint=%s method=%s account=%s allowed=%s auth=%s",
                       request.endpoint, request.method, account, allowed,
                       "local" if local else "token" if allowed else "denied")
    if not allowed:
        return jsonify({"error": "画像/知识库管理需要独立管理员授权"}), 403
    return None

_state = {"last_sync": 0, "last_sync_result": {}, "syncing": False}
_lock = threading.Lock()

_bot = {"thread": None, "running": False, "log": [], "state": {}}
_bot_lifecycle_lock = threading.RLock()


def _start_bot():
    """One worker per process. A user stop pauses it without losing its queue."""
    with _bot_lifecycle_lock:
        _bot['running'] = True
        if _bot['thread'] is None or not _bot['thread'].is_alive():
            _bot['thread'] = threading.Thread(target=_bot_loop, daemon=True)
            _bot['thread'].start()


def _msg_db_mtime():
    """消息库(含 -wal)最新修改时间，微信一写消息/撤回就会变。"""
    try:
        base = os.path.join(config.db_storage_dir(), "message", "message_0.db")
        m = os.path.getmtime(base) if os.path.exists(base) else 0.0
        wal = base + "-wal"
        if os.path.exists(wal):
            m = max(m, os.path.getmtime(wal))
        return m
    except Exception:  # noqa: BLE001
        return 0.0


# 焦点会话看护：持久地把"要盯防撤回的会话"钉在微信里打开(被切走就重开)，并快轮询该会话
# →get_messages 触发 _resolve_revokes 缓存清晰图。这样新图到达微信瞬间自动下清晰 _b.dat，
# 秒撤前就缓存到清晰版。仅在"保持会话打开"开关开启时工作。
_focus = {"chat": None, "name": None, "thread": None}


def _focus_loop():
    """Retired: reading a conversation must not navigate the WeChat window."""
    return


def _ensure_focus_loop():
    return


# 图片存档：后台把监听会话/焦点会话里收到的图解密成高清图永久存档,并(限速)读图内容。
# 清晰度依赖是否已下全图(配合"保持会话打开"/路线A注入);拿不到高清就先存现有版,以后升级。
_arch = {"thread": None, "seen": set()}


_arch_lastfetch = {}          # chat -> 上次为它做UI抓全图的时间(去抖)


def _archiver_loop():
    from core import imgarchive, imgdec, decrypt as _dec, messages as _msg, harvest
    import time as _t
    while True:
        try:
            rules = botmod.load_rules()
            fullres_on = False   # 开关:允许自动开会话抓全图
            chats = list(dict.fromkeys(_bexpand(rules.get("watch", []))
                                       + ([_focus["chat"]] if _focus.get("chat") else [])))
            _dec.run(force=False, only=["message"])
            need_fullres = []      # [(chat, display)] 有缩略图-only 新图、需UI补全图的会话
            now = _t.time()
            for chat in chats:
                if not chat or chat == "*":
                    continue
                try:
                    ms = _msg.get_messages(chat, limit=25)
                except Exception:  # noqa: BLE001
                    continue
                chat_needs = False
                for m in ms:
                    if m.get("type") != 3 or not m.get("local_id"):
                        continue
                    if (m.get("create_time") or 0) < now - 86400:   # 只处理近一天的图
                        continue
                    key = (chat, m["local_id"])
                    r = imgarchive.archive_image(chat, m["local_id"], meta=m)
                    if r.get("ok"):
                        if r.get("thumb"):
                            chat_needs = True         # 还只是缩略图→标记需补全图
                        else:
                            _arch["seen"].add(key)    # 只存字节(便宜,revoke-proof);
                            # 视觉读图(desc)不再每收图就调,改为按需(检索/点"读图内容"/机器人回复时)
                if chat_needs:
                    need_fullres.append((chat, imgdec._display_name(chat)))
            # 事件驱动补全图:每轮挑一个"有缩略图新图"的会话,自动开它让微信下全图(串行、去抖、
            # 让位发送)。这样很多群/私聊也能自动轮到,不用手点。开关关闭时只存现有清晰度。
            if fullres_on and need_fullres:
                cand = sorted(need_fullres, key=lambda c: _arch_lastfetch.get(c[0], 0))
                for chat, disp in cand[:1]:           # 每轮补一个会话(下一轮换下一个)
                    if _t.time() - _arch_lastfetch.get(chat, 0) < 45:
                        continue
                    _arch_lastfetch[chat] = _t.time()
                    try:
                        harvest.pull_latest_fullres(disp, log=lambda *_: None)
                        _t.sleep(0.5)
                        for m in _msg.get_messages(chat, limit=25):
                            if m.get("type") == 3 and m.get("local_id") \
                                    and (m.get("create_time") or 0) > now - 86400:
                                imgarchive.archive_image(chat, m["local_id"], meta=m)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
        _t.sleep(20)


def _bexpand(watch):
    """展开 watch 里的 '*' 为所有群(复用 bot 的展开)。"""
    try:
        return botmod._expand_watch(watch)
    except Exception:  # noqa: BLE001
        return [w for w in watch if w != "*"]


def _ensure_archiver():
    if _arch["thread"] is None or not _arch["thread"].is_alive():
        t = threading.Thread(target=_archiver_loop, daemon=True)
        t.start()
        _arch["thread"] = t


def _bot_loop():
    import config as _cfg
    from core import reply_inbox
    reply_inbox.start(lambda: _bot['running'])
    from core import local_media
    local_media.start()
    cur_session = sessions.observe()
    _bot["state"] = botmod.load_state()
    botmod.load_pending()        # 恢复去抖待回队列:重启前正等待回复的消息不丢,启动后补回

    def log(msg):
        _bot["log"].append(msg)
        _bot["log"][:] = _bot["log"][-100:]
        print("[bot]", msg)

    while True:
        if not _bot['running']:
            time.sleep(0.2)
            continue
        try:
            # 切微信账号自动重载:登录账号变了→丢掉旧账号的内存 state,读新账号自己的
            # bot_state/pending。否则新账号会沿用旧账号的高位 last_seen,消息全被当"已读"不回。
            observed_session = sessions.observe()
            if observed_session != cur_session:
                log("检测到账号代次变化，重载监听状态")
                cur_session = observed_session
                _bot["state"] = botmod.load_state()
                botmod.load_pending()
            rules = botmod.load_rules()
            botmod.run_once(rules, _bot["state"], log=log)
        except Exception as e:  # noqa: BLE001
            log(f"error: {e}")
        # 事件驱动等待：盯着消息库 -wal 的 mtime，微信一写就立刻再解析(≈0.35s 内)，
        # 而不是死等 5 秒。poll_interval 仅作兜底最大间隔。
        try:
            cap = float(botmod.load_rules().get("poll_interval", 3))
        except Exception:  # noqa: BLE001
            cap = 3.0
        base_mtime = _msg_db_mtime()
        waited = 0.0
        while _bot["running"] and waited < cap:
            time.sleep(0.35)
            waited += 0.35
            if _msg_db_mtime() != base_mtime:   # 库有变动 → 立刻处理
                break


# ---------------- 同步（解密刷新） ----------------
def do_sync(force=False):
    with _lock:
        if _state["syncing"]:
            return _state["last_sync_result"]
        _state["syncing"] = True
    try:
        res = decrypt.run(force=force)
        _state["last_sync"] = time.time()
        _state["last_sync_result"] = res
        return res
    finally:
        _state["syncing"] = False


def poller():
    """事件驱动同步：盯着消息库(-wal)的 mtime，微信一写(任何会话的文字/图片/撤回)
    就【立刻】增量解密刷新解密库(≈0.3s 内),让网页/机器人/按需接口都能马上读到最新
    消息——而不是死等固定间隔。另设 8s 兜底最大间隔(catch 联系人/会话库等非消息库变动,
    并防 mtime 偶发漏检)。decrypt.run(force=False) 是增量的(解密库比源新就跳过),无变动
    时几乎零成本,故可 0.3s 高频探测。配合 Frida 秒抢图片字节 = 文字秒到 + 图片撤回不丢。"""
    last_mtime = -1.0
    last_full = 0.0
    while True:
        sessions.observe()
        try:
            if os.path.exists(config.keys_json()):
                now = time.time()
                m = _msg_db_mtime()
                if m != last_mtime or (now - last_full) >= 8:
                    do_sync(force=False)
                    last_mtime = m
                    last_full = now
        except Exception as e:  # noqa: BLE001
            print("poller error:", e)
        time.sleep(0.3)


# ---------------- API ----------------
@app.get("/api/status")
def api_status():
    keys_ready = False
    core_keys = []
    if os.path.exists(config.keys_json()):
        try:
            with open(config.keys_json()) as f:
                keys = json.load(f)
            core_keys = [k for k, v in config.CORE_DBS.items() if v in keys]
            # 只要求【磁盘上确实存在的】核心库都有密钥——media_0.db 等在没收到语音/视频前
            # 根本不存在,不该因此报"缺密钥"。db_storage 拿不到时退回宽松判断(有message密钥即可)。
            dbs = config.db_storage_dir()
            if dbs:
                required = {v for v in config.CORE_DBS.values()
                            if os.path.exists(os.path.join(dbs, v))}
            else:
                required = set()
            if required:
                keys_ready = required.issubset(set(keys.keys()))
            else:
                keys_ready = config.CORE_DBS.get("message", "message/message_0.db") in keys \
                    or "message/message_0.db" in keys
        except Exception:  # noqa: BLE001
            pass
    decrypted_ready = os.path.exists(config.decrypted_path("message"))
    from core import imgdec
    return jsonify({
        "img_ready": imgdec.images_available() or bool(imgdec.img_key()),
        "container_running": docker_wx.container_running(),
        "wechat_running": docker_wx.wechat_running(),
        "logged_in": docker_wx.logged_in(),
        "logged_in_wxid": docker_wx.container_wxid(),
        "novnc_url": os.environ.get("WXBOT_NOVNC_URL", "http://localhost:6080/vnc.html"),
        "keys_ready": keys_ready,
        "core_keys": core_keys,
        "decrypted_ready": decrypted_ready,
        "last_sync": _state["last_sync"],
        "syncing": _state["syncing"],
    })

@app.get('/accounts')
def accounts_page():
    return send_from_directory(os.path.join(config.PROJECT_DIR, 'static'), 'accounts.html')

def _accounts_auth():
    expected = os.environ.get('WXBOT_ADMIN_READ_TOKEN', '')
    supplied = request.headers.get('X-Wxbot-Admin-Token', '')
    import hmac
    return bool(expected and hmac.compare_digest(expected.encode(), supplied.encode())) or local_management_access()

from core.moments_api import create_blueprint as create_moments_blueprint
app.register_blueprint(create_moments_blueprint(_accounts_auth))

@app.get('/moments')
def moments_page():
    return send_from_directory(os.path.join(config.PROJECT_DIR, 'static'), 'moments.html')

@app.before_request
def _protect_account_manager():
    if request.path.startswith('/api/accounts') and not _accounts_auth():
        return jsonify(error='多账号管理需要管理员授权'), 403

@app.get('/api/accounts')
def api_accounts():
    from core import multi_account
    return jsonify(accounts=multi_account.list_accounts())

@app.post('/api/accounts')
def api_accounts_create():
    from core import multi_account
    body=request.get_json(silent=True) or {}
    if not isinstance(body.get('label'), str) or not body['label'].strip(): return jsonify(error='label 不能为空'),400
    try: return jsonify(ok=True, account=multi_account.create(body['label'], body.get('id')))
    except (ValueError, KeyError) as exc: return jsonify(error=str(exc)),400

@app.post('/api/accounts/<account_id>/<operation>')
def api_accounts_action(account_id, operation):
    from core import multi_account
    try: return jsonify(multi_account.action(account_id, operation))
    except (ValueError, KeyError) as exc: return jsonify(error=str(exc)),400

@app.post('/api/accounts/batch')
def api_accounts_batch():
    from core import multi_account
    action=request.get_json(silent=True).get('action') if request.get_json(silent=True) else ''
    if action not in ('start','stop','restart'): return jsonify(error='不支持的批量操作'),400
    return jsonify(results=[multi_account.action(a['id'], action) for a in multi_account.list_accounts()])


@app.post("/api/login")
def api_login():
    """返回 noVNC 地址，让用户在浏览器里扫码登录容器里的微信。"""
    running = docker_wx.container_running()
    return jsonify({
        "ok": running,
        "novnc_url": os.environ.get("WXBOT_NOVNC_URL", "http://localhost:6080/vnc.html"),
        "logged_in": docker_wx.logged_in(),
        "msg": ("打开 noVNC 用小号扫码登录" if running else "容器未运行，请先启动容器"),
    })


@app.post("/api/keys/refresh")
def api_keys_refresh():
    """重新从容器内存提取密钥并解密（微信重启后 key 会变）。"""
    ok, log = docker_wx.refresh_keys()
    if ok:
        do_sync(force=True)
    return jsonify({"ok": ok, "log": log})


@app.post("/api/keys/capture_img")
def api_capture_img_key():
    """注入微信抓账号级图片AES密钥(切号后用)。抓取期间暂停机器人+驱动UI触发图片解密。"""
    from core import imgdec
    if imgdec.img_key() and not (request.get_json(force=True, silent=True) or {}).get("force"):
        return jsonify({"ok": True, "key": "已有(加 force 可重抓)"})
    # capture_img_key already holds the shared UI_LOCK. Do not change the
    # user's run/stop choice or start a second polling thread for UI maintenance.
    try:
        ok, res = docker_wx.capture_img_key(log=lambda m: _bot["log"].append(m))
    except Exception as e:  # noqa: BLE001
        ok, res = False, str(e)
    return jsonify({"ok": ok, "key": (res[:10] + "…") if ok else res})


@app.get("/api/bot")
def api_bot_status():
    try:
        rules = botmod.load_rules()
    except Exception as e:  # noqa: BLE001
        rules = {"error": str(e)}
    from core import sender, reply_inbox
    return jsonify({"running": _bot["running"], "rules": rules,
                    "message_reader": reply_inbox.status(),
                    "sending_available": bool(sender._adapter.available),
                    "sending_scope": getattr(sender._adapter, "scope", "private_text" if isinstance(sender._adapter, sender.NativeContactAdapter) else "unknown"),
                    "log": _bot["log"][-30:]})


@app.get("/api/categories")
def api_categories():
    """消息分类字典(slug->中文)。供按类型配置规则/前端筛选用。"""
    from core import msgclass
    return jsonify({"categories": msgclass.CATEGORIES})


@app.post("/api/bot/start")
def api_bot_start():
    _start_bot()
    return jsonify({"ok": True, "running": True})


@app.post("/api/bot/stop")
def api_bot_stop():
    with _bot_lifecycle_lock:
        _bot["running"] = False
    return jsonify({"ok": True, "running": False})


# ---------------- 蒸馏 / 人设 / LLM ----------------
from core import distill, llm  # noqa: E402


@app.get("/api/llm")
def api_llm_status():
    cfg = llm.load_cfg()

    def one(p):
        base, key, model = llm.creds(cfg, p)
        return {"base_url": base, "model": model, "has_key": bool(key)}
    return jsonify({"configured": llm.available(),
                    "provider": cfg.get("provider", "claude"),
                    "vision_provider": cfg.get("vision_provider", ""),
                    "tools_provider": cfg.get("tools_provider", ""),
                    "tools_model": cfg.get("tools_model", ""),
                    "tool_route": llm.route_info(cfg, "tools"),
                    "private_management_auth": True,
                    "contact_personalization": True,
                    "route_diagnostics": llm.route_diagnostics(),
                    "proxy": cfg.get("proxy", ""),
                    "claude": one("claude"), "gpt": one("gpt")})


@app.post("/api/llm/test")
def api_llm_test():
    """分别实测 Claude / GPT 两套中转 + 视觉是否可用。"""
    cfg = llm.load_cfg()
    out = {}
    for p in ("claude", "gpt"):
        base, key, model = llm.creds(cfg, p)
        if not key:
            out[p] = "未配置"
            continue
        try:
            c = dict(cfg)
            c["provider"] = p
            r = llm.chat("只回OK两个字", [{"role": "user", "content": "OK"}], c)
            out[p] = f"✅ 通 ({model})" if r else "⚠️ 空响应"
        except Exception as e:  # noqa: BLE001
            out[p] = "❌ " + str(e)[:80]
    # 视觉(按 vision_provider 或主 provider)
    try:
        import io as _io
        vp = cfg.get("vision_provider") or cfg.get("provider", "claude")
        png = _io.BytesIO()
        try:
            from PIL import Image
            Image.new("RGB", (32, 32), (200, 100, 50)).save(png, "PNG")
            data = png.getvalue()
        except Exception:  # noqa: BLE001
            data = None
        if data:
            d = llm.describe_image(data, media_type="image/png", prompt="一句话这是什么颜色", cfg=cfg)
            out["vision"] = (f"✅ 通 ({vp})" if d else "❌ 无响应")
        else:
            out["vision"] = "跳过(无PIL)"
    except Exception as e:  # noqa: BLE001
        out["vision"] = "❌ " + str(e)[:80]
    return jsonify(out)


@app.post("/api/llm/config")
def api_llm_config():
    body = request.get_json(force=True, silent=True) or {}
    cfg = llm.load_cfg()
    for k in ("provider", "vision_provider", "tools_provider"):
        choices = ("claude", "gpt") if k == "provider" else ("", "claude", "gpt")
        if k in body and body[k] not in choices:
            return jsonify({"error": "不支持的 provider"}), 400
    if "tools_model" in body and not isinstance(body["tools_model"], str):
        return jsonify({"error": "工具模型必须是文本"}), 400
    for k in ("tools_provider", "tools_model"):
        if k in body:
            cfg[k] = body[k].strip()
    for k in ("provider", "vision_provider", "proxy", "max_tokens", "temperature"):
        if k in body:
            cfg[k] = body[k]
    # 两套独立中转：claude / gpt 各存 base_url/api_key/model；key 留空则不覆盖旧值
    for p in ("claude", "gpt"):
        sub = body.get(p)
        if isinstance(sub, dict):
            cur = cfg.get(p) if isinstance(cfg.get(p), dict) else {}
            if sub.get("base_url") is not None:
                cur["base_url"] = sub["base_url"].strip()
            if sub.get("model"):
                cur["model"] = sub["model"].strip()
            if sub.get("api_key"):           # 只有填了才更新，留空保留原 key
                cur["api_key"] = sub["api_key"].strip()
            cfg[p] = cur
    with open(llm.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True, "configured": llm.available()})


@app.get("/api/personas")
def api_personas():
    return jsonify(distill.list_personas())


@app.get("/api/personas/<slug>")
def api_persona_detail(slug):
    p = distill.load_persona(slug)
    if not p:
        return jsonify({"error": "不存在"}), 404
    return jsonify(p)


@app.post("/api/personas/import")
def api_persona_import():
    b = request.get_json(force=True, silent=True) or {}
    name = (b.get("name") or "").strip()
    persona = (b.get("persona") or "").strip()
    if not name or not persona:
        return jsonify({"ok": False, "error": "缺少 名字/人设内容"}), 400
    samples = b.get("samples") or []
    if isinstance(samples, str):
        samples = [s for s in samples.splitlines() if s.strip()]
    d = distill.import_persona(name, persona, samples, b.get("slug"))
    return jsonify({"ok": True, "slug": d["slug"]})


@app.post("/api/personas/delete")
def api_persona_delete():
    slug = (request.get_json(force=True, silent=True) or {}).get("slug", "")
    return jsonify({"ok": distill.delete_persona(slug)})


@app.post("/api/personas/<slug>/correct")
def api_persona_correct(slug):
    """纠正精修：把用户反馈并入人设并重渲染(不必重跑蒸馏)。"""
    fb = (request.get_json(force=True, silent=True) or {}).get("feedback", "").strip()
    if not fb:
        return jsonify({"ok": False, "error": "缺少 feedback"}), 400
    p = distill.correct(slug, fb)
    if not p:
        return jsonify({"ok": False, "error": "人设不存在"}), 404
    return jsonify({"ok": True, "persona": p.get("persona")})


@app.post("/api/bot/watch")
def api_bot_watch():
    """设置机器人监听的会话列表(群+私聊)。"""
    watch = (request.get_json(force=True, silent=True) or {}).get("watch", [])
    rules = botmod.load_rules()
    rules["watch"] = list(dict.fromkeys(watch))
    with open(botmod.rules_file(), "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True, "watch": rules["watch"]})

@app.get("/api/bot/proactive")
def api_bot_proactive_get():
    rules = botmod.load_rules()
    return jsonify(rules.get('proactive') or {})

@app.post("/api/bot/proactive")
def api_bot_proactive_set():
    body = request.get_json(force=True, silent=True) or {}
    rules = botmod.load_rules()
    old = rules.get('proactive') or {}
    out = {**old}
    for k in ('enabled','private_share_enabled','group_enabled'):
        if k in body: out[k] = bool(body[k])
    for k in ('after','gap','max','group_after','quiet_start','quiet_end'):
        if k in body: out[k] = max(0, int(body[k]))
    rules['proactive'] = out
    with open(botmod.rules_file(), 'w', encoding='utf-8') as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
    return jsonify({'ok': True, 'proactive': out})


@app.post("/api/focus")
def api_focus():
    body = request.get_json(force=True, silent=True) or {}
    _focus['chat'] = body.get('chat')
    _focus['name'] = body.get('name')
    return jsonify(ok=True, focus=_focus['chat'], ui_navigation=False)


@app.get("/api/bot/fullres")
def api_fullres_get():
    return jsonify(enabled=False, mode='local_hook', ui_navigation=False)


@app.post("/api/bot/fullres")
def api_fullres_set():
    return jsonify(ok=True, enabled=False, mode='local_hook', ui_navigation=False,
                   message='媒体已改为本地读取与进程捕获')


@app.get("/api/bot/follow")
def api_bot_follow_get():
    """读取群消息跟发(接龙/+1)配置。"""
    try:
        rules = botmod.load_rules()
    except Exception as e:  # noqa: BLE001
        return jsonify({"groups": [], "threshold": 3, "error": str(e)})
    return jsonify({"groups": rules.get("follow_watch", []),
                    "threshold": rules.get("follow_threshold", 3)})


@app.post("/api/bot/follow")
def api_bot_follow():
    """设置开启跟发的群 + 触发阈值(连续相同几次触发)。"""
    body = request.get_json(force=True, silent=True) or {}
    groups = [g for g in body.get("groups", []) if g.endswith("@chatroom")]
    rules = botmod.load_rules()
    rules["follow_watch"] = list(dict.fromkeys(groups))
    if "threshold" in body:
        try:
            rules["follow_threshold"] = max(2, int(body["threshold"]))
        except (TypeError, ValueError):
            pass
    with open(botmod.rules_file(), "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True, "groups": rules["follow_watch"],
                    "threshold": rules.get("follow_threshold", 3)})


def _default_push():
    return {"enabled": False, "sources": [], "include_self": False, "targets": []}


@app.get("/api/bot/push")
def api_bot_push_get():
    """读取监听推送配置(把监听会话的任何消息推给微信好友/webhook)。"""
    try:
        rules = botmod.load_rules()
    except Exception as e:  # noqa: BLE001
        return jsonify({**_default_push(), "error": str(e)})
    p = rules.get("push") or {}
    return jsonify({**_default_push(), **p})


@app.post("/api/bot/push")
def api_bot_push_set():
    """设置监听推送：enabled/sources(缺省=监听列表)/include_self/targets。
    targets: [{"type":"wechat","to":"好友名"},{"type":"webhook","url":"https://..."}]"""
    body = request.get_json(force=True, silent=True) or {}
    rules = botmod.load_rules()
    p = rules.get("push") or {}
    if "enabled" in body:
        p["enabled"] = bool(body["enabled"])
    if "include_self" in body:
        p["include_self"] = bool(body["include_self"])
    if "sources" in body:
        p["sources"] = list(dict.fromkeys(body.get("sources") or []))
    if "targets" in body:
        clean = []
        for t in body.get("targets") or []:
            if not isinstance(t, dict):
                continue
            typ = t.get("type")
            if typ == "wechat" and (t.get("to") or "").strip():
                to = t["to"].strip()
                clean.append({"type": "wechat", "to": to,
                              "username": t.get("username")
                              or botmod._username_for_name(to),
                              "enabled": t.get("enabled", True)})
            elif typ == "webhook" and (t.get("url") or "").strip():
                clean.append({"type": "webhook", "url": t["url"].strip(),
                              "enabled": t.get("enabled", True)})
        p["targets"] = clean
    rules["push"] = {**_default_push(), **p}
    with open(botmod.rules_file(), "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True, "push": rules["push"]})


@app.post("/api/bot/push/test")
@sessions.task
def api_bot_push_test():
    """用当前(或请求体传入的)推送目标发一条测试消息,验证配置连通。"""
    body = request.get_json(force=True, silent=True) or {}
    rules = botmod.load_rules()
    p = dict(rules.get("push") or {})
    if body.get("targets"):
        p["targets"] = body["targets"]
    logs = []
    fake = {"type": 1, "category": "text", "category_name": "文字",
            "content": body.get("text") or "【wxbot 推送测试】这是一条测试消息",
            "sender": "", "is_self": False, "local_id": 0}
    botmod._do_push(fake, body.get("chat") or "filehelper", p, logs.append)
    return jsonify({"ok": True, "log": logs})


@app.post("/api/bot/persona")
def api_bot_persona():
    # Explicit migration is the sole new global role write path. The old UI must
    # not claim success while silently changing an inactive legacy rule.
    return jsonify({"ok": False, "error": "请在联系人画像与专属人设页面核对迁移并设置全局人设"}), 409


# ---------------- 知识库(RAG) ----------------
@app.get("/api/kb")
def api_kb_list():
    from core import knowledge
    return jsonify(knowledge.list_docs())


@app.post("/api/kb/add")
def api_kb_add():
    from core import knowledge
    b = request.get_json(force=True, silent=True) or {}
    content = (b.get("content") or "").strip()
    if not content:
        return jsonify({"ok": False, "error": "content 为空"}), 400
    if b.get("chunk"):
        n = knowledge.import_text(content, source=b.get("source", "import"),
                                  title_prefix=b.get("title", ""))
        return jsonify({"ok": True, "chunks": n})
    knowledge.add(b.get("title", ""), content, b.get("source", "manual"))
    return jsonify({"ok": True})


@app.post("/api/kb/delete")
def api_kb_delete():
    from core import knowledge
    b = request.get_json(force=True, silent=True) or {}
    knowledge.delete(source=b.get("source"), doc_id=b.get("id"))
    return jsonify({"ok": True})


@app.get("/api/kb/search")
def api_kb_search():
    from core import knowledge
    q = request.args.get("q", "")
    return jsonify({"hits": knowledge.search(q, int(request.args.get("k", 5)))})


# ---------------- 人物画像 / 长期记忆 ----------------
@app.get("/api/profiles")
def api_profiles():
    from core import memory
    return jsonify({"profiles": memory.list_profiles()})


@app.get("/api/profiles/<path:wxid>")
def api_profile_detail(wxid):
    from core import memory
    return jsonify(memory.load_profile(wxid))


@app.post("/api/profiles/<path:wxid>/delete")
def api_profile_delete(wxid):
    from core import memory
    import os as _os
    try:
        _os.remove(memory._path(wxid))
    except OSError:
        pass
    return jsonify({"ok": True})


# ---------------- Agent 开关/工具白名单 ----------------
@app.get("/api/agent/config")
def api_agent_config_get():
    from core import agent, tools
    rules = botmod.load_rules()
    cfg = agent.agent_config(rules)
    return jsonify({"enabled": cfg["enabled"], "tools": cfg["tools"],
                    "all_tools": [s["name"] for s in tools.SPECS]})


@app.post("/api/agent/config")
def api_agent_config_set():
    b = request.get_json(force=True, silent=True) or {}
    rules = botmod.load_rules()
    rules["agent"] = {"enabled": bool(b.get("enabled")),
                      "tools": b.get("tools") or []}
    with open(botmod.rules_file(), "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True, "agent": rules["agent"]})


# ---------------- 图片高清存档 ----------------
@app.get("/api/archive")
def api_archive_list():
    from core import imgarchive
    return jsonify({"stats": imgarchive.stats(),
                    **imgarchive.list_archive(limit=int(request.args.get("limit", 200)))})


@app.get("/api/archive/img")
def api_archive_img():
    from core import imgarchive
    chat = request.args.get("chat", "")
    lid = request.args.get("id")
    if not chat or not lid:
        return Response(status=400)
    data = imgarchive.archived_bytes(chat, int(lid))
    if not data:
        return Response(status=404)
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


@app.post("/api/archive/describe")
def api_archive_describe():
    """按需用视觉模型读存档图内容(不再每收图就调)。
    传 {chat,id} 读单张；传 {all:true,limit:N} 补齐最近 N 张未读的。"""
    from core import imgarchive
    body = request.get_json(silent=True) or {}
    if body.get("all"):
        n = imgarchive.describe_undescribed(limit=int(body.get("limit", 12)))
        return jsonify({"ok": True, "described": n})
    chat = body.get("chat", "")
    lid = body.get("id")
    if not chat or lid is None:
        return jsonify({"ok": False, "error": "need chat+id"}), 400
    d = imgarchive.describe(chat, int(lid), force=bool(body.get("force")))
    return jsonify({"ok": bool(d), "desc": d})


@app.get("/api/archive/search")
def api_archive_search():
    from core import imgarchive
    q = request.args.get("q", "")
    # 检索即"需要"→对最近未读图按需补 desc(有上限,避免一次性全量调用)
    if q and request.args.get("describe", "1") != "0":
        imgarchive.describe_undescribed(limit=int(request.args.get("dmax", 12)))
    return jsonify({"hits": imgarchive.search(q, int(request.args.get("k", 20)))})


# ---------------- 定时任务(自然语言) ----------------
@app.get("/api/schedule")
def api_schedule_list():
    from core import schedule
    return jsonify({"tasks": schedule.list_view(), "log": schedule.logs()[-20:]})


@app.post("/api/schedule/nl")
@sessions.task
def api_schedule_nl():
    """自然语言创建/取消/列出定时任务。"""
    from core import schedule
    text = (request.get_json(force=True, silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "message": "描述为空"}), 400
    return jsonify(schedule.handle_nl(text))


@app.post("/api/greet")
@sessions.task
def api_greet():
    """网页"打招呼"：按该会话最近上下文,用人设生成一句问候并发送。"""
    b = request.get_json(force=True, silent=True) or {}
    chat = b.get("chat")
    if not chat:
        return jsonify({"ok": False, "message": "缺 chat"}), 400
    try:
        res = botmod.greet(chat)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(e)})
    return jsonify(res)


@app.post("/api/schedule/<int:tid>/delete")
def api_schedule_delete(tid):
    from core import schedule
    n = schedule.remove_task(id=tid)
    return jsonify({"ok": n > 0})


@app.post("/api/schedule/<int:tid>/update")
def api_schedule_update(tid):
    """编辑任务(改时间/内容/目标等)。body=要改的字段(cron/once_at/prompt/title/target/mention/use_llm)。"""
    from core import schedule
    fields = request.get_json(force=True, silent=True) or {}
    t, err = schedule.update_task(id=tid, fields=fields)
    if err:
        return jsonify({"ok": False, "message": err}), 400
    return jsonify({"ok": True, "task": t,
                    "schedule_desc": schedule.describe_schedule(t)})


@app.post("/api/schedule/<int:tid>/run")
@sessions.task
def api_schedule_run(tid):
    """立即手动触发一次(测试用)。"""
    from core import schedule
    for t in schedule.load_tasks():
        if t.get("id") == tid:
            threading.Thread(target=schedule.fire, args=(t,),
                kwargs={"session": sessions.capture()}, daemon=True).start()
            return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "任务不存在"}), 404


@app.post("/api/distill/candidates")
def api_distill_candidates():
    g = (request.get_json(force=True, silent=True) or {}).get("group")
    if not g:
        return jsonify({"error": "缺少 group"}), 400
    return jsonify(distill.candidates(g))


@app.post("/api/distill/run")
def api_distill_run():
    b = request.get_json(force=True, silent=True) or {}
    if not b.get("group") or not b.get("wxid"):
        return jsonify({"ok": False, "error": "缺少 group/wxid"}), 400
    if not llm.available():
        return jsonify({"ok": False, "error": "未配置 LLM(先在 llm_config.json 填 api_key)"}), 400
    try:
        data, msg = distill.run(b["group"], b["wxid"], b.get("name"))
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    if not data:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "slug": data["slug"], "name": data["name"],
                    "stats": data["stats"], "persona": data["persona"]})


@app.post("/api/distill/self")
def api_distill_self():
    """蒸馏自己：汇总所有群聊+私聊里自己发的话。"""
    if not llm.available():
        return jsonify({"ok": False, "error": "未配置 LLM(先在 AI设置 填 api_key)"}), 400
    b = request.get_json(force=True, silent=True) or {}
    try:
        data, msg = distill.run_self(b.get("name"))
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    if not data:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "slug": data["slug"], "name": data["name"],
                    "stats": data["stats"], "persona": data["persona"]})


@app.post("/api/sync")
def api_sync():
    res = do_sync(force=bool(request.args.get("force")))
    return jsonify({"ok": True, "result": res})


_namecache = {"t": 0, "map": {}}


def _name_map():
    now = time.time()
    if now - _namecache["t"] < 30 and _namecache["map"]:
        return _namecache["map"]
    m = {}
    try:
        for u, info in contacts.all_names().items():
            m[u] = {"name": info["name"], "head": info.get("head_url")}
        for g in contacts.list_groups():
            m[g["username"]] = {"name": g["name"], "head": g.get("head_url")}
    except Exception:  # noqa: BLE001
        pass
    _namecache["t"] = now
    _namecache["map"] = m
    return m


_OTHER_ACCOUNTS = {
    "weixin", "filehelper", "fmessage", "medianote", "floatbottle", "newsapp",
    "qqmail", "tmessage", "qmessage", "mphelper", "brandsessionholder",
    "notification_messages", "notifymessage", "exmail_tool", "officialaccounts",
    "helper_entry", "weixinreminder", "qqsync", "blogapp", "masssendapp",
    "shakeapp", "lbsapp", "voipapp", "voicevoipapp",
}


def _is_other(username, name=""):
    """判断会话是否属于「其他」(公众号/服务号/系统/官方通知)，而非私聊/群聊。"""
    u = username or ""
    if u.endswith("@chatroom"):
        return False                      # 群聊
    if u.startswith("gh_"):
        return True                       # 公众号/服务号
    if u in _OTHER_ACCOUNTS:
        return True                       # 系统账号
    if any(sb in u for sb in ("sessionholder", "brandservice", "notification",
                              "@brand")):
        return True                       # 品牌/服务/通知类占位会话
    if "@openim" in u:
        return False                      # 企业微信真人联系人
    if name in ("公众号", "服务号", "订阅号", "微信团队", "微信支付", "腾讯新闻"):
        return True
    return False                          # 其余=私聊


# 折叠占位会话：只是镜像被折叠账号的最新一条，与真实账号重复，直接不显示
_HOLDER_ACCOUNTS = {"brandsessionholder", "brandservicesessionholder",
                    "opencustomerservicemsg", "notification_messages2"}


def _is_holder(u):
    return u in _HOLDER_ACCOUNTS or "sessionholder" in (u or "")


@app.get("/api/sessions")
def api_sessions():
    try:
        decrypt.run(force=False, only=["session"])   # 保证未读数等是最新(变了才重解密)
    except Exception:  # noqa: BLE001
        pass
    try:
        ss = messages.list_sessions()
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 409
    ss = [s for s in ss if not _is_holder(s["username"])]   # 去掉折叠占位重复项
    nm = _name_map()
    for s in ss:
        info = nm.get(s["username"], {})
        s["name"] = info.get("name") or s["username"]
        s["head_url"] = info.get("head")
        s["category"] = "other" if _is_other(s["username"], s["name"]) else "chat"
    return jsonify(ss)


@app.get("/api/contacts")
def api_contacts():
    try:
        return jsonify(contacts.list_contacts())
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 409


@app.get("/api/groups")
def api_groups():
    try:
        return jsonify(contacts.list_groups())
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 409


@app.get("/api/groups/<path:chatroom>/members")
def api_group_members(chatroom):
    try:
        return jsonify(contacts.group_members(chatroom))
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 409


@app.get("/api/messages")
def api_messages():
    chat = request.args.get("chat")
    if not chat:
        return jsonify({"error": "缺少 chat 参数"}), 400
    limit = int(request.args.get("limit", 50))
    before = request.args.get("before")
    before = int(before) if before else None
    try:
        decrypt.run(force=False, only=["message", "biz_message"])   # 普通与公众号消息均保持刷新
    except Exception:  # noqa: BLE001
        pass
    try:
        rows=messages.get_messages(chat, limit=limit, before=before)
        from core import local_media
        states=local_media.observe(chat,rows)
        for row in rows:
            if row.get('type') in (3,34,43):
                state=states.get(row.get('local_id'),{})
                row['media_status']=state.get('status','pending')
                row['media_label']=local_media.LABELS.get(row['media_status'],'等待媒体就绪')
                row['media_updated']=state.get('updated',0)
        return jsonify(rows)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 409


@app.get('/static/avatar-placeholder.svg')
def avatar_placeholder():
    return send_file(os.path.join(config.PROJECT_DIR, 'static', 'avatar-placeholder.svg'))


@app.get("/api/avatar")
def api_avatar():
    username = request.args.get("username", "")
    data, mime = avatars.get_avatar(username)
    if not data:
        if username == 'newsapp':
            return send_file(os.path.join(config.PROJECT_DIR, 'static', 'news-avatar.svg'))
        info = contacts.all_names().get(username, {})
        url = info.get('head_url') or ''
        if url.startswith(('https://', 'http://')):
            from flask import redirect
            return redirect(url)
        return send_file(os.path.join(config.PROJECT_DIR, 'static', 'avatar-placeholder.svg'))
    return send_file(io.BytesIO(data), mimetype=mime)


@app.get("/api/msgimage")
def api_msgimage():
    from core import imgdec
    chat = request.args.get("chat", "")
    lid = request.args.get("id")
    if not chat or not lid:
        return Response(status=400)
    data, mime = imgdec.get_msg_image(chat, int(lid))
    if not data:
        return Response(status=404)
    return send_file(io.BytesIO(data), mimetype=mime)


@app.post("/api/harvest")
def api_harvest():
    # Cached older pages may still call this endpoint. They cannot drive UI.
    return jsonify(ok=True, decrypted=0, mode='local_hook', ui_navigation=False,
                   log=['已停用界面翻图，媒体由本地读取与进程捕获提供'])


@app.get('/api/media/status')
def api_media_status():
    from core import local_media
    return jsonify(local_media.hook_status())


@app.post('/api/media/refresh')
def api_media_refresh():
    from core import local_media
    body = request.get_json(force=True, silent=True) or {}
    chat = body.get('chat')
    if not isinstance(chat,str) or not chat or len(chat)>256:
        return jsonify(error='请选择一个聊天'),400
    rows=messages.get_messages(chat,limit=60)
    states=local_media.observe(chat,rows,reset=True)
    local_media.start()
    return jsonify(ok=True, queued=len(states), ui_navigation=False)


@app.get("/api/msgvoice")
def api_msgvoice():
    from core import media
    chat = request.args.get("chat", "")
    lid = request.args.get("id")
    if not chat or not lid:
        return Response(status=400)
    data, mime = media.get_msg_voice(chat, int(lid))
    if not data:
        return Response(status=404)
    return send_file(io.BytesIO(data), mimetype=mime)


@app.get("/api/msgvideo")
def api_msgvideo():
    from core import media
    chat = request.args.get("chat", "")
    lid = request.args.get("id")
    if not chat or not lid:
        return Response(status=400)
    data, mime = media.get_msg_video(chat, int(lid))
    if not data:
        return Response(status=404)
    return send_file(io.BytesIO(data), mimetype=mime)


@app.get("/api/msgvideothumb")
def api_msgvideothumb():
    from core import media
    chat = request.args.get("chat", "")
    lid = request.args.get("id")
    if not chat or not lid:
        return Response(status=404)
    data, mime = media.get_msg_video_thumb(chat, int(lid))
    if not data:
        return Response(status=404)
    return send_file(io.BytesIO(data), mimetype=mime)


@app.post("/api/send")
@sessions.task
def api_send():
    """Create a durable send job; receipt uncertainty never triggers automatic resend."""
    from core import sendq
    body = request.get_json(force=True, silent=True) or {}
    to = body.get("to")
    kind = body.get("type", "text")
    chat = body.get("chat")            # stable target requested; UI proof is still required
    if not to:
        return jsonify({"ok": False, "error": "缺少 to（会话显示名）"}), 400
    content = body.get("content", "")
    path = body.get("path", "")
    if kind == "text" and not content:
        return jsonify({"ok": False, "error": "content 为空"}), 400
    if kind == "image" and (not path or not os.path.exists(path)):
        return jsonify({"ok": False, "error": f"图片不存在：{path}"}), 400
    if kind not in ("text", "image"):
        return jsonify({"ok": False, "error": "type 必须是 text/image"}), 400
    if body.get("sync"):
        try:
            res = (sender.send_text(to, content, chat_username=chat) if kind == "text"
                   else sender.send_image(to, path, chat_username=chat))
            return jsonify(res), (200 if res.get("ok") else 500)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 500
    job_id, ahead = sendq.enqueue(kind, to, chat, content, path)
    return jsonify({"ok": True, "queued": True, "job": job_id, "ahead": ahead})


@app.get("/api/send/status")
def api_send_status():
    from core import sendq
    return jsonify(sendq.status(request.args.get("job", "")))


@app.post("/api/upload")
def api_upload():
    """接收前端上传的图片，落地到 work/uploads，返回本地路径供 /api/send 使用。"""
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "无文件"}), 400
    f = request.files["file"]
    updir = os.path.join(config.WORK_DIR, "uploads")
    os.makedirs(updir, exist_ok=True)
    path = os.path.join(updir, f.filename)
    f.save(path)
    return jsonify({"ok": True, "path": path})


# ---------------- 静态前端 ----------------
@app.get("/")
def index():
    return send_from_directory(os.path.join(config.PROJECT_DIR, "static"), "index.html")


def main():
    config.ensure_dirs()
    from core import moments
    moments.start_loop()
    from core import sender
    if isinstance(sender._adapter, sender.NativeContactAdapter) and sender._adapter.available:
        sessions.install_identity_probe(lambda: sender._adapter.ui.call('state')['signature'])
    t = threading.Thread(target=poller, daemon=True)
    t.start()
    # 机器人随后台自启（有 reply_ai 人设或规则时）
    try:
        if os.environ.get('WXBOT_BOT_AUTOSTART', '1') == '1':
            _start_bot()
            print("机器人已自启")
    except Exception as e:  # noqa: BLE001
        print("机器人自启失败:", e)
    try:
        from core import schedule
        schedule.start_loop()             # 定时任务调度线程随后台自启
    except Exception as e:  # noqa: BLE001
        print("定时任务调度自启失败:", e)
    try:
        _ensure_archiver()                # 图片高清存档线程随后台自启
    except Exception as e:  # noqa: BLE001
        print("图片存档自启失败:", e)
    bind = os.environ.get("WXBOT_BIND", "127.0.0.1")  # 容器内设 0.0.0.0，宿主默认只本地
    print(f"wxbot 后台启动： http://localhost:{config.PORT} (bind {bind})")
    app.run(host=bind, port=config.PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
