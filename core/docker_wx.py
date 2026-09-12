"""与 Docker 容器里的 Linux 微信交互：状态 / 取密钥 / 发送 / 截图。"""
import json
import os
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from core import account_session as sessions

CONTAINER = os.environ.get("WXBOT_CONTAINER", "wxbot")

# 部署两种形态：
#  - 宿主模式(默认，macOS/开发)：后端在宿主，经 `docker exec <容器>` 操作微信容器
#  - 容器内模式(WXBOT_LOCAL=1，服务器单容器部署)：后端与微信同容器，命令直接本地执行
LOCAL = os.environ.get("WXBOT_LOCAL") == "1"
# 容器内微信数据根目录
LOCAL_XWECHAT = os.environ.get("WXBOT_XWECHAT_ROOT", "/root/xwechat_files")

# 微信只有一个 UI 窗口：机器人发消息 与 图片解密(翻图) 都靠 xdotool 操作它，
# 必须串行，否则互相插入按键会彼此搞乱。两边都用这把锁。
from core.ui_lock import UILock
UI_LOCK = UILock() if LOCAL else threading.RLock()

# 发送优先：翻图解密(harvest)是长耗时后台活，会长时间占着微信 UI。发送必须能立刻插队，
# 否则用户点发送会一直卡"发送中"。发送前 request_priority()，harvest 每轮检查 priority_pending()
# 若有发送在等就立刻让位(提前结束、松锁)。
_send_pending = 0
_send_pending_lock = threading.Lock()


def request_priority():
    global _send_pending
    with _send_pending_lock:
        _send_pending += 1


def release_priority():
    global _send_pending
    with _send_pending_lock:
        _send_pending = max(0, _send_pending - 1)


def priority_pending():
    return _send_pending > 0


# 当前微信里打开着哪个会话(wxid)。用于"保持监控会话打开"→新图自动被微信下成清晰版
# (对当前打开会话，微信收到图会自动下清晰 _b.dat，无需点开、秒撤也来得及)。
_open_chat = {"chat": None}


def note_open(chat_username):
    _open_chat["chat"] = chat_username


def clear_open():
    _open_chat["chat"] = None


def current_open():
    return _open_chat["chat"]


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


@sessions.task
def capture_img_key(log=lambda m: None, trigger=True):
    with UI_LOCK:
        return _capture_img_key_locked(log, trigger)


def _capture_img_key_locked(log=lambda m: None, trigger=True):
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


@sessions.task
def refresh_keys():
    with UI_LOCK:
        return _refresh_keys_locked()


def _refresh_keys_locked():
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
    # The extractor prints a summary even when it found zero keys.  Treat
    # zero hits as failure; otherwise the UI claims success and later reports
    # a misleading missing-session.db error.
    summary = re.search(r"命中\s+(\d+)\s*/\s*(\d+)", r.stdout + r.stderr)
    ok = bool(summary and int(summary.group(1)) > 0)
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
    from core import sender
    return sender.send_text(name, text)


def open_chat(name):
    """只打开会话、不发送(供发送前截图核对标题)。"""
    with UI_LOCK:
        r = _exec("python3", "/usr/local/bin/wx_send.py", "justopen", name, timeout=30)
    return "OK" in r.stdout


def search_query(name):
    """在侧栏搜索框输入查询、弹出结果下拉(不回车)。"""
    with UI_LOCK:
        r = _exec("python3", "/usr/local/bin/wx_send.py", "searchquery", name, timeout=30)
    return "OK" in r.stdout


def click(cx, cy):
    """点击绝对屏幕坐标。"""
    with UI_LOCK:
        r = _exec("python3", "/usr/local/bin/wx_send.py", "clickxy",
                  str(int(cx)), str(int(cy)), timeout=20)
    return "OK" in r.stdout


def paste_text(text):
    from core import sender
    return sender.send_text('', text)


def title_shot(host_out):
    """截当前微信窗口(含会话标题)到 host_out，返回是否成功。"""
    cpath = host_out if LOCAL else "/tmp/_titleshot.png"
    with UI_LOCK:
        _exec("python3", "/usr/local/bin/wx_send.py", "titleshot", cpath, timeout=20)
    if not LOCAL:
        _docker("cp", f"{CONTAINER}:{cpath}", host_out)
    return os.path.exists(host_out)


def paste_at(member_name, text):
    from core import sender
    return sender.send_at('', None, None, member_name, text)


def paste_image_open(host_path):
    from core import sender
    return sender.send_image('', host_path)


def send_image(name, host_path):
    from core import sender
    return sender.send_image(name, host_path)


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
