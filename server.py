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

import config
from core import decrypt, contacts, messages, avatars, docker_wx, sender
from core import bot as botmod

app = Flask(__name__, static_folder=None)

_state = {"last_sync": 0, "last_sync_result": {}, "syncing": False}
_lock = threading.Lock()

_bot = {"thread": None, "running": False, "log": [], "state": {}}


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
    from core import decrypt as _dec, messages as _msg, sender as _snd
    while True:
        chat = _focus["chat"]
        name = _focus["name"]
        try:
            if not chat or not botmod.load_rules().get("fullres_capture"):
                time.sleep(1.5)
                continue
            # 被切走(发送/翻图/别的会话)就重开，保持钉住
            if not docker_wx.priority_pending() and docker_wx.current_open() != chat:
                _snd.focus_chat(name, chat)
            # 快轮询该会话：get_messages 内部会缓存(升级)清晰图。收紧到 ~0.5s 提高抓到
            # "撤回前那一刻清晰图已就位"的命中率(微信下 _b.dat 约需1秒)。
            _dec.run(force=False, only=["message"])
            _msg.get_messages(chat, limit=15)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)


def _ensure_focus_loop():
    if _focus["thread"] is None or not _focus["thread"].is_alive():
        t = threading.Thread(target=_focus_loop, daemon=True)
        t.start()
        _focus["thread"] = t


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
            fullres_on = bool(rules.get("fullres_capture"))   # 开关:允许自动开会话抓全图
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
    _bot["state"] = botmod.load_state()

    def log(msg):
        _bot["log"].append(msg)
        _bot["log"][:] = _bot["log"][-100:]
        print("[bot]", msg)

    while _bot["running"]:
        try:
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
        "novnc_url": "http://localhost:6080/vnc.html",
        "keys_ready": keys_ready,
        "core_keys": core_keys,
        "decrypted_ready": decrypted_ready,
        "last_sync": _state["last_sync"],
        "syncing": _state["syncing"],
    })


@app.post("/api/login")
def api_login():
    """返回 noVNC 地址，让用户在浏览器里扫码登录容器里的微信。"""
    running = docker_wx.container_running()
    return jsonify({
        "ok": running,
        "novnc_url": "http://localhost:6080/vnc.html",
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
    was = _bot["running"]
    _bot["running"] = False
    time.sleep(0.6)
    try:
        ok, res = docker_wx.capture_img_key(log=lambda m: _bot["log"].append(m))
    except Exception as e:  # noqa: BLE001
        ok, res = False, str(e)
    finally:
        if was and not _bot["running"]:
            _bot["running"] = True
            _bot["thread"] = threading.Thread(target=_bot_loop, daemon=True)
            _bot["thread"].start()
    return jsonify({"ok": ok, "key": (res[:10] + "…") if ok else res})


@app.get("/api/bot")
def api_bot_status():
    try:
        rules = botmod.load_rules()
    except Exception as e:  # noqa: BLE001
        rules = {"error": str(e)}
    return jsonify({"running": _bot["running"], "rules": rules,
                    "log": _bot["log"][-30:]})


@app.get("/api/categories")
def api_categories():
    """消息分类字典(slug->中文)。供按类型配置规则/前端筛选用。"""
    from core import msgclass
    return jsonify({"categories": msgclass.CATEGORIES})


@app.post("/api/bot/start")
def api_bot_start():
    if not _bot["running"]:
        _bot["running"] = True
        _bot["thread"] = threading.Thread(target=_bot_loop, daemon=True)
        _bot["thread"].start()
    return jsonify({"ok": True, "running": True})


@app.post("/api/bot/stop")
def api_bot_stop():
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


@app.post("/api/focus")
def api_focus():
    """把网页正在看的会话设为"焦点会话"——后台看护线程会持续把它钉在微信里打开并快轮询，
    新图到达微信自动下清晰版(_b.dat)，秒撤前就缓存到清晰图。仅在"保持会话打开"开关开启时生效。"""
    b = request.get_json(force=True, silent=True) or {}
    chat = b.get("chat")
    name = b.get("name") or (botmod.send_name_for(chat) if chat else None)
    _focus["chat"] = chat
    _focus["name"] = name
    if botmod.load_rules().get("fullres_capture"):
        _ensure_focus_loop()
    return jsonify({"ok": True, "focus": chat})


@app.get("/api/bot/fullres")
def api_fullres_get():
    """读取"保持会话打开(抓清晰图)"开关。"""
    try:
        return jsonify({"enabled": bool(botmod.load_rules().get("fullres_capture"))})
    except Exception:  # noqa: BLE001
        return jsonify({"enabled": False})


@app.post("/api/bot/fullres")
def api_fullres_set():
    body = request.get_json(force=True, silent=True) or {}
    rules = botmod.load_rules()
    rules["fullres_capture"] = bool(body.get("enabled"))
    with open(botmod.rules_file(), "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
    if rules["fullres_capture"]:
        _ensure_focus_loop()
    return jsonify({"ok": True, "enabled": rules["fullres_capture"]})


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


@app.post("/api/bot/persona")
def api_bot_persona():
    """把某人设设为机器人 style-reply 规则的回复人设。"""
    slug = (request.get_json(force=True, silent=True) or {}).get("slug", "")
    if not distill.load_persona(slug):
        return jsonify({"ok": False, "error": "人设不存在"}), 400
    rules = botmod.load_rules()
    found = False
    for r in rules.get("rules", []):
        if r.get("action", {}).get("type") == "reply_ai":
            r["action"]["persona"] = slug
            found = True
    if not found:
        rules.setdefault("rules", []).insert(0, {
            "name": "style-reply", "match": {"type": "mention"},
            "action": {"type": "reply_ai", "persona": slug}})
    with open(botmod.rules_file(), "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True, "persona": slug})


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
def api_schedule_nl():
    """自然语言创建/取消/列出定时任务。"""
    from core import schedule
    text = (request.get_json(force=True, silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "message": "描述为空"}), 400
    return jsonify(schedule.handle_nl(text))


@app.post("/api/schedule/<int:tid>/delete")
def api_schedule_delete(tid):
    from core import schedule
    n = schedule.remove_task(id=tid)
    return jsonify({"ok": n > 0})


@app.post("/api/schedule/<int:tid>/run")
def api_schedule_run(tid):
    """立即手动触发一次(测试用)。"""
    from core import schedule
    for t in schedule.load_tasks():
        if t.get("id") == tid:
            threading.Thread(target=schedule.fire, args=(t,), daemon=True).start()
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
        decrypt.run(force=False, only=["message"])   # 新消息实时可见(变了才重解密)
    except Exception:  # noqa: BLE001
        pass
    try:
        return jsonify(messages.get_messages(chat, limit=limit, before=before))
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 409


@app.get("/api/avatar")
def api_avatar():
    username = request.args.get("username", "")
    data, mime = avatars.get_avatar(username)
    if not data:
        return Response(status=404)
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
    """驱动容器微信翻遍某会话的历史图片→触发微信解密落盘→网页即可显示。
    期间暂停机器人(共用同一个微信窗口)，完成后恢复。"""
    from core import harvest as hv
    body = request.get_json(force=True, silent=True) or {}
    username = body.get("chat", "")
    name = None
    if username:
        try:
            name = botmod.send_name_for(username)
        except Exception:  # noqa: BLE001
            name = None
    was_running = _bot["running"]
    _bot["running"] = False           # 暂停机器人，避免抢微信窗口
    time.sleep(0.6)
    logs = []
    try:
        nav = int(body.get("nav") or 60)
        got = hv.harvest(name, nav=max(6, min(nav, 80)), log=lambda m: logs.append(str(m)))
    except Exception as e:  # noqa: BLE001
        got = 0
        logs.append(f"出错: {e}")
    finally:
        if was_running and not _bot["running"]:
            _bot["running"] = True
            _bot["thread"] = threading.Thread(target=_bot_loop, daemon=True)
            _bot["thread"].start()
    return jsonify({"ok": True, "decrypted": got, "log": logs})


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
def api_send():
    """入发送队列(单 worker FIFO 串行)，立即返回 job；发送含视觉核对+发后落库校验耗时
    十几秒，同步等会卡界面，故异步+队列。同步模式(body.sync)供脚本。"""
    from core import sendq
    body = request.get_json(force=True, silent=True) or {}
    to = body.get("to")
    kind = body.get("type", "text")
    chat = body.get("chat")            # 会话 wxid：传了就做发后校验(确认落到正确会话)
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
    t = threading.Thread(target=poller, daemon=True)
    t.start()
    # 机器人随后台自启（有 reply_ai 人设或规则时）
    try:
        _bot["running"] = True
        _bot["thread"] = threading.Thread(target=_bot_loop, daemon=True)
        _bot["thread"].start()
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
