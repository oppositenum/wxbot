"""与 Docker 容器里的 Linux 微信交互：状态 / 取密钥 / 发送 / 截图。"""
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

CONTAINER = os.environ.get("WXBOT_CONTAINER", "wxbot")

# 部署两种形态：
#  - 宿主模式(默认，macOS/开发)：后端在宿主，经 `docker exec <容器>` 操作微信容器
#  - 容器内模式(WXBOT_LOCAL=1，服务器单容器部署)：后端与微信同容器，命令直接本地执行
LOCAL = os.environ.get("WXBOT_LOCAL") == "1"
# 容器内微信数据根目录
LOCAL_XWECHAT = os.environ.get("WXBOT_XWECHAT_ROOT", "/root/xwechat_files")

# 微信只有一个 UI 窗口：机器人发消息 与 图片解密(翻图) 都靠 xdotool 操作它，
# 必须串行，否则互相插入按键会彼此搞乱。两边都用这把锁。
UI_LOCK = threading.RLock()


def _docker(*args, timeout=60):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout)


def _exec(*args, timeout=60):
    """在“微信所在环境”里执行命令：容器内=直接跑；宿主=docker exec。"""
    cmd = list(args) if LOCAL else ["docker", "exec", CONTAINER, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _xwechat_base():
    return LOCAL_XWECHAT if LOCAL else os.path.join(config.DOCKER_DATA, "xwechat_files")


def container_running():
    if LOCAL:
        return True
    r = _docker("ps", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}")
    return CONTAINER in r.stdout.split()


def container_wxid():
    """当前账号 wxid（数据目录里 contact.db 最新的那个，兼容多账号残留）。"""
    base = _xwechat_base()
    if not os.path.isdir(base):
        return None
    best, best_m = None, -1
    for name in os.listdir(base):
        c = os.path.join(base, name, "db_storage", "contact", "contact.db")
        if os.path.exists(c):
            m = os.path.getmtime(c)
            if m > best_m:
                best, best_m = name, m
    return best


def wechat_running():
    if not container_running():
        return False
    r = _exec("pgrep", "-f", "/opt/wechat/wechat")
    return bool(r.stdout.strip())


def logged_in():
    return container_wxid() is not None and wechat_running()


def refresh_keys():
    """跑 linux_keys.py 提取密钥，写到本账号 keys.json。"""
    wxid = container_wxid()
    if not wxid:
        return False, "未检测到已登录账号"
    dbs = f"{_xwechat_base()}/{wxid}/db_storage" if LOCAL \
        else f"/root/xwechat_files/{wxid}/db_storage"
    out_keys = config.keys_json() if LOCAL else "/root/keys.json"
    if LOCAL:
        os.makedirs(os.path.dirname(out_keys), exist_ok=True)
    r = _exec("python3", "/usr/local/bin/linux_keys.py", dbs, out_keys, timeout=120)
    ok = "命中" in (r.stdout + r.stderr)
    if ok and not LOCAL:
        # 宿主模式：容器写到 /root/keys.json(=docker/wxdata/keys.json)，拷到本账号目录
        import shutil
        src = os.path.join(config.DOCKER_DATA, "keys.json")
        if os.path.exists(src):
            os.makedirs(os.path.dirname(config.keys_json()), exist_ok=True)
            shutil.copy(src, config.keys_json())
    return ok, (r.stdout + r.stderr)[-500:]


def send_text(name, text):
    with UI_LOCK:
        r = _exec("python3", "/usr/local/bin/wx_send.py", "text", name, text,
                  timeout=40)
    out = (r.stdout + r.stderr).strip()
    if "OK" in r.stdout:
        return {"ok": True}
    return {"ok": False, "error": out or "send failed"}


def send_image(name, host_path):
    if not os.path.exists(host_path):
        return {"ok": False, "error": f"图片不存在: {host_path}"}
    if LOCAL:
        cpath = host_path                         # 同一文件系统，无需拷贝
    else:
        base = os.path.basename(host_path)
        cpath = f"/tmp/wxsend_{int(os.path.getmtime(host_path))}_{base}"
        cp = _docker("cp", host_path, f"{CONTAINER}:{cpath}")
        if cp.returncode != 0:
            return {"ok": False, "error": "docker cp 失败: " + cp.stderr}
    with UI_LOCK:
        r = _exec("python3", "/usr/local/bin/wx_send.py", "image", name, cpath,
                  timeout=60)
    if "OK" in r.stdout:
        return {"ok": True}
    return {"ok": False, "error": (r.stdout + r.stderr).strip() or "send failed"}


def screenshot(host_out):
    """截图到 host_out。"""
    if LOCAL:
        _exec("bash", "-c", f"DISPLAY=:0 scrot -o {host_out}")
    else:
        _docker("exec", CONTAINER, "bash", "-c", "DISPLAY=:0 scrot -o /tmp/_shot.png")
        _docker("cp", f"{CONTAINER}:/tmp/_shot.png", host_out)
    return os.path.exists(host_out)


if __name__ == "__main__":
    print("container_running:", container_running())
    print("wxid:", container_wxid())
    print("wechat_running:", wechat_running())
    print("logged_in:", logged_in())
