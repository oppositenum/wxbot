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
from core import decrypt, contacts, messages, avatars, docker_wx
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
    while True:
        try:
            if os.path.exists(config.keys_json()):
                do_sync(force=False)
        except Exception as e:  # noqa: BLE001
            print("poller error:", e)
        time.sleep(10)


# ---------------- API ----------------
@app.get("/api/status")
def api_status():
    keys_ready = False
    core_keys = []
    if os.path.exists(config.keys_json()):
        try:
            with open(config.keys_json()) as f:
                keys = json.load(f)
            core_vals = set(config.CORE_DBS.values())
            core_keys = [k for k, v in config.CORE_DBS.items() if v in keys]
            keys_ready = core_vals.issubset(set(keys.keys()))
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
    return jsonify({"configured": llm.available(), "provider": cfg.get("provider"),
                    "model": cfg.get("model"), "gpt_model": cfg.get("gpt_model"),
                    "base_url": cfg.get("base_url", ""), "proxy": cfg.get("proxy", "")})


@app.post("/api/llm/config")
def api_llm_config():
    body = request.get_json(force=True, silent=True) or {}
    cfg = llm.load_cfg()
    for k in ("provider", "api_key", "model", "gpt_model", "proxy",
              "max_tokens", "temperature", "base_url"):
        if k in body:
            cfg[k] = body[k]
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


@app.post("/api/bot/watch")
def api_bot_watch():
    """设置机器人监听的会话列表(群+私聊)。"""
    watch = (request.get_json(force=True, silent=True) or {}).get("watch", [])
    rules = botmod.load_rules()
    rules["watch"] = list(dict.fromkeys(watch))
    with open(botmod.rules_file(), "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True, "watch": rules["watch"]})


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
    body = request.get_json(force=True, silent=True) or {}
    to = body.get("to")
    kind = body.get("type", "text")
    if not to:
        return jsonify({"ok": False, "error": "缺少 to（会话显示名）"}), 400
    try:
        if kind == "text":
            content = body.get("content", "")
            if not content:
                return jsonify({"ok": False, "error": "content 为空"}), 400
            res = docker_wx.send_text(to, content)
        elif kind == "image":
            path = body.get("path", "")
            if not path or not os.path.exists(path):
                return jsonify({"ok": False, "error": f"图片不存在：{path}"}), 400
            res = docker_wx.send_image(to, path)
        else:
            return jsonify({"ok": False, "error": "type 必须是 text/image"}), 400
        code = 200 if res.get("ok") else 500
        return jsonify(res), code
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


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
    bind = os.environ.get("WXBOT_BIND", "127.0.0.1")  # 容器内设 0.0.0.0，宿主默认只本地
    print(f"wxbot 后台启动： http://localhost:{config.PORT} (bind {bind})")
    app.run(host=bind, port=config.PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
