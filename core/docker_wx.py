"""与 Docker 容器里的 Linux 微信交互：状态 / 取密钥 / 发送 / 截图。"""
import json
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


def capture_img_key(log=lambda m: None, trigger=True):
    """注入微信抓账号级图片 AES 密钥(存 keys.json 的 _img_key)。切号后可重跑。
    需在抓取期间触发图片显示(冷缓存时打开会话即可)。返回 (ok, key_hex_or_msg)。"""
    import json
    import shutil
    import time as _t
    pid_cmd = "pgrep -x wechat | head -1"
    r = _exec("bash", "-lc", pid_cmd)
    pid = (r.stdout or "").strip()
    if not pid:
        return False, "微信未运行"
    _exec("bash", "-lc", "pkill -9 gdb 2>/dev/null; rm -f /tmp/cap_ready /root/imgkey.txt")
    # 后台起 gdb 捕获脚本
    if LOCAL:
        subprocess.Popen(["bash", "-lc",
                          f"gdb -p {pid} -batch -x /usr/local/bin/capture_imgkey.py "
                          f"> /tmp/capk.log 2>&1"])
    else:
        _docker("exec", "-d", CONTAINER, "bash", "-lc",
                f"gdb -p {pid} -batch -x /usr/local/bin/capture_imgkey.py > /tmp/capk.log 2>&1")
    for _ in range(20):                       # 等 gdb 就绪
        if _exec("bash", "-lc", "[ -f /tmp/cap_ready ] && echo r").stdout.strip():
            break
        _t.sleep(1)
    if trigger:                               # 触发图片 .dat 读取(滚动若干会话)
        with UI_LOCK:
            for y in (160, 232, 300, 370, 440):
                _exec("bash", "-lc", f"DISPLAY=:0 xdotool mousemove 340 {y} click 1")
                _t.sleep(0.7)
                _exec("bash", "-lc", "DISPLAY=:0 xdotool mousemove 800 400; "
                      "for j in 1 2 3 4; do DISPLAY=:0 xdotool click 4; sleep 0.15; done")
    for _ in range(20):                       # 等抓取结果
        out = _exec("bash", "-lc", "cat /root/imgkey.txt 2>/dev/null").stdout.strip()
        if out:
            keys_path = config.keys_json()
            os.makedirs(os.path.dirname(keys_path), exist_ok=True)
            d = {}
            if os.path.exists(keys_path):
                try:
                    d = json.load(open(keys_path))
                except Exception:  # noqa: BLE001
                    d = {}
            d["_img_key"] = out
            json.dump(d, open(keys_path, "w"), ensure_ascii=False, indent=2)
            log(f"图片密钥已抓取并保存: {out[:8]}…")
            return True, out
        _t.sleep(1)
    _exec("bash", "-lc", "pkill -9 gdb 2>/dev/null")
    tail = _exec("bash", "-lc", "tail -3 /tmp/capk.log").stdout
    return False, "未抓到(可能图片已缓存未触发解密;切号后冷缓存更易抓)。日志:" + tail[-200:]


def _preserve_img_key(dst):
    """linux_keys.py 只产 DB 密钥、会整体覆盖 keys.json，而 _img_key 是账号级持久值
    (微信重启不变)——刷新 DB 密钥后必须把它补回，否则收到的图片会全部解不开。"""
    try:
        return json.load(open(dst)).get("_img_key")
    except Exception:  # noqa: BLE001
        return None


def _restore_img_key(dst, img):
    if not img:
        return
    try:
        d = json.load(open(dst))
        if not d.get("_img_key"):
            d["_img_key"] = img
            json.dump(d, open(dst, "w"), ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def refresh_keys():
    """跑 linux_keys.py 提取密钥，写到本账号 keys.json。保留已存的 _img_key。"""
    wxid = container_wxid()
    if not wxid:
        return False, "未检测到已登录账号"
    dbs = f"{_xwechat_base()}/{wxid}/db_storage" if LOCAL \
        else f"/root/xwechat_files/{wxid}/db_storage"
    out_keys = config.keys_json() if LOCAL else "/root/keys.json"
    if LOCAL:
        os.makedirs(os.path.dirname(out_keys), exist_ok=True)
    img = _preserve_img_key(config.keys_json())          # 刷新前记住图片密钥
    r = _exec("python3", "/usr/local/bin/linux_keys.py", dbs, out_keys, timeout=120)
    ok = "命中" in (r.stdout + r.stderr)
    if ok and not LOCAL:
        # 宿主模式：容器写到 /root/keys.json(=docker/wxdata/keys.json)，拷到本账号目录
        import shutil
        src = os.path.join(config.DOCKER_DATA, "keys.json")
        if os.path.exists(src):
            os.makedirs(os.path.dirname(config.keys_json()), exist_ok=True)
            shutil.copy(src, config.keys_json())
    if ok:
        _restore_img_key(config.keys_json(), img)        # 刷新后补回图片密钥
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
