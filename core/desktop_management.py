"""Manual desktop management; does not import account readers or send workers."""
from pathlib import Path
import os
import subprocess
from urllib.parse import urlsplit

from flask import Blueprint, Flask, has_request_context, jsonify, request, send_from_directory


# This process is one WeChat. The page never lists other accounts.
DEFAULT_PROFILES = {
    "primary": {"id": "primary", "name": "当前微信号", "container": "wxbot-ubuntu-manual",
                "system": "Ubuntu 24.04 · XFCE", "home": "/home/wechat"},
}
STATIC = Path(__file__).resolve().parents[1] / "static"


def own_profile():
    profile = dict(DEFAULT_PROFILES["primary"])
    raw = os.environ.get("WXBOT_NOVNC_PORT", "").strip()
    if raw:
        try:
            port = int(raw)
        except ValueError:
            port = 0
        if 0 < port <= 65535:
            profile["port"] = port
    return profile


def _page_hostname():
    """Hostname from the request Host, including bracketed IPv6. Never rsplit."""
    if not has_request_context():
        return None
    host = request.host or ""
    if host != host.strip() or "/" in host or "@" in host or "\\" in host:
        return None
    name = urlsplit("//" + host).hostname
    if not name or name == "0.0.0.0":
        return None
    if any(c in name for c in " /?#@"):
        return None
    return name


def desktop_url(profile):
    public_url = os.environ.get("WXBOT_PUBLIC_NOVNC_URL", "").strip()
    if public_url:
        return public_url
    raw = os.environ.get("WXBOT_NOVNC_PORT", "").strip()
    if not raw:
        return ""
    try:
        port = int(raw)
    except ValueError:
        return ""
    if port <= 0 or port > 65535:
        return ""
    name = _page_hostname()
    if not name:
        return ""
    bracket = f"[{name}]" if ":" in name else name
    return (f"http://{bracket}:{port}/vnc.html"
            "?autoconnect=true&resize=scale&view_only=false&path=websockify")


def _host_agent_post(op, container):
    """Ask the host helper. None means it is unreachable."""
    import json as _json
    import urllib.request
    from core.multi_account import HOST_AGENT_URL, _host_agent_token
    token = _host_agent_token()
    if not token:
        return None
    body = _json.dumps({"token": token, "op": op, "container": container}).encode("utf-8")
    req = urllib.request.Request(
        HOST_AGENT_URL.rstrip("/") + "/docker", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return _json.loads(resp.read() or b"{}")
    except Exception:
        return None


def probe(profile):
    """Only inspect the named container and process. Neither implies login."""
    result = dict(profile, desktop_url=desktop_url(profile), container_running=False,
                  wechat_running=False, login_state="unknown", error=None)
    container = profile["container"]
    try:
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container],
            capture_output=True, text=True, timeout=4)
        if inspected.returncode:
            result["error"] = "未找到容器，或 Docker 暂不可用"
            return result
        result["container_running"] = inspected.stdout.strip() == "true"
        if result["container_running"]:
            process = subprocess.run(
                ["docker", "exec", container, "pgrep", "-f", "/opt/wechat/wechat"],
                capture_output=True, text=True, timeout=4)
            if process.returncode not in (0, 1):
                result["error"] = "暂时无法检查微信进程"
            result["wechat_running"] = process.returncode == 0 and bool(process.stdout.strip())
    except FileNotFoundError:
        # No docker CLI inside the container. Ask the host agent instead of
        # reporting a dead desktop (the page shows 「桌面未运行」 when error is empty).
        agent = _host_agent_post("inspect", container)
        if not agent or not agent.get("ok"):
            result["error"] = "宿主状态服务不可用"
            return result
        result["container_running"] = bool(agent.get("running"))
        if not result["container_running"]:
            return result
        wechat = _host_agent_post("wechat", container)
        if not wechat or not wechat.get("ok"):
            result["error"] = "宿主状态服务不可用"
            return result
        result["wechat_running"] = bool(wechat.get("running"))
    except (OSError, subprocess.TimeoutExpired):
        result["error"] = "连接 Docker 超时或失败"
    return result


bp = Blueprint("desktop_management", __name__)


@bp.get("/desktop")
def desktop_page():
    return send_from_directory(STATIC, "desktop.html")


@bp.get("/api/desktop/instances")
def instances():
    profile = own_profile()
    row = dict(profile, desktop_url=desktop_url(profile))
    return jsonify(default_instance=profile["id"], mode="manual_desktop", instances=[row])


def selected_profile():
    # One container, one desktop. A client instance= must not switch profiles.
    return own_profile()


@bp.get("/api/desktop/status")
def instance_status():
    profile = selected_profile()
    if profile is None:
        return jsonify(error="未知微信实例"), 400
    return jsonify(probe(profile))


def create_app():
    """Serve the existing management port without loading automation modules.

    A stale chat tab must fail explicitly, rather than reach the old sender or
    account database. Selection is per browser request, never global mutable state.
    """
    app = Flask(__name__, static_folder=None)
    app.register_blueprint(bp)

    @app.before_request
    def desktop_only():
        allowed = {"/api/desktop/instances", "/api/desktop/status", "/api/status",
                   "/api/login", "/api/bot"}
        if request.path.startswith("/api/") and request.path not in allowed:
            return jsonify(ok=False, error="当前为微信桌面管理模式，请刷新后台后在桌面内操作。"
                           "聊天数据、发送接口和自动回复尚未接入。",
                           code="desktop_only", mode="manual_desktop"), 409

    @app.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return desktop_page()

    @app.get("/api/status")
    def status():
        profile = selected_profile()
        if profile is None:
            return jsonify(error="未知微信实例"), 400
        state = probe(profile)
        return jsonify(**state, mode="manual_desktop", novnc_url=state["desktop_url"],
                       logged_in=None, logged_in_wxid=None, keys_ready=False,
                       decrypted_ready=False, last_sync=0, syncing=False)

    @app.post("/api/login")
    def login():
        profile = selected_profile()
        if profile is None:
            return jsonify(error="未知微信实例"), 400
        return jsonify(ok=True, instance=profile["id"], novnc_url=desktop_url(profile),
                       logged_in=None, msg="请在微信桌面查看登录状态")

    @app.get("/api/bot")
    def bot():
        return jsonify(running=False, sending_available=False, sending_scope="manual_desktop",
                       log=[], rules={}, mode="manual_desktop")

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5100, debug=False, threaded=True,
                     use_reloader=False)
