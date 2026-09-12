"""Manual desktop management; does not import account readers or send workers."""
from pathlib import Path
import subprocess

from flask import Blueprint, Flask, jsonify, request, send_from_directory


PROFILES = {
    "ubuntu": {"id": "ubuntu", "name": "Ubuntu 微信", "container": "wxbot-ubuntu-manual",
               "port": 6082, "system": "Ubuntu 24.04 · XFCE", "home": "/home/wechat"},
    "legacy": {"id": "legacy", "name": "原 Debian 微信", "container": "wxbot",
               "port": 6080, "system": "Debian 12", "home": "/root"},
}
STATIC = Path(__file__).resolve().parents[1] / "static"


def desktop_url(profile):
    return (f"http://localhost:{profile['port']}/vnc.html"
            "?autoconnect=true&resize=scale&view_only=false")


def probe(profile):
    """Only inspect the named container and process. Neither implies login."""
    result = dict(profile, desktop_url=desktop_url(profile), container_running=False,
                  wechat_running=False, login_state="unknown", error=None)
    try:
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", profile["container"]],
            capture_output=True, text=True, timeout=4)
        if inspected.returncode:
            result["error"] = "未找到容器，或 Docker 暂不可用"
            return result
        result["container_running"] = inspected.stdout.strip() == "true"
        if result["container_running"]:
            process = subprocess.run(
                ["docker", "exec", profile["container"], "pgrep", "-x", "wechat"],
                capture_output=True, text=True, timeout=4)
            if process.returncode not in (0, 1):
                result["error"] = "暂时无法检查微信进程"
            result["wechat_running"] = process.returncode == 0 and bool(process.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        result["error"] = "连接 Docker 超时或失败"
    return result


bp = Blueprint("desktop_management", __name__)


@bp.get("/desktop")
def desktop_page():
    return send_from_directory(STATIC, "desktop.html")


@bp.get("/api/desktop/instances")
def instances():
    return jsonify(default_instance="ubuntu", mode="manual_desktop",
                   instances=[dict(p, desktop_url=desktop_url(p)) for p in PROFILES.values()])


def selected_profile():
    return PROFILES.get(request.args.get("instance", "ubuntu"))


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
