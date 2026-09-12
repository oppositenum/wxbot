"""全局配置与路径自动探测。"""
import os
import glob
import pwd
import re


def _real_home():
    """在 sudo 下 ~ 会变成 /var/root，这里回退到真实登录用户的家目录。"""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        try:
            return pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


HOME = _real_home()

# 微信 4.x (xwechat) 容器数据根
CONTAINER = os.path.join(
    HOME,
    "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files",
)

# 本项目工作目录
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
WORK_DIR = os.path.join(PROJECT_DIR, "work")
# 每个微信账号的数据分开存：accounts/<wxid>/{keys.json,decrypted/,personas/,bot_*.json}
ACCOUNTS_DIR = os.path.join(PROJECT_DIR, "accounts")

# Docker 容器挂载出来的数据目录（Linux 微信）
DOCKER_DATA = os.path.join(PROJECT_DIR, "docker", "wxdata")


# 容器内单容器部署时，微信数据在 /root/xwechat_files；宿主模式在 docker/wxdata
_XWECHAT_ROOT = os.environ.get("WXBOT_XWECHAT_ROOT", "/root/xwechat_files")


def _detect_docker_db_storage():
    """在 docker/wxdata(宿主) 或 /root/xwechat_files(容器内) 里找已登录账号的 db_storage。"""
    bases = [os.path.join(DOCKER_DATA, "xwechat_files"), _XWECHAT_ROOT]
    best, best_m = None, -1
    for base in bases:
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            dbs = os.path.join(base, name, "db_storage")
            c = os.path.join(dbs, "contact", "contact.db")
            if os.path.exists(c):
                m = os.path.getmtime(c)
                if m > best_m:
                    best, best_m = dbs, m
    return best


# 显式覆盖(环境变量)；其余走动态探测(容器内登录发生在后端启动之后，不能在导入期缓存)
_ENV_DB_STORAGE = os.environ.get("WXBOT_DB_STORAGE")


def _resolve_db_storage():
    return _ENV_DB_STORAGE or _detect_docker_db_storage()

# 微信主进程名（读内存提密钥用）
WECHAT_PROC = "WeChat"

# 后台端口
PORT = 5100

# 核心库相对 db_storage 的路径
CORE_DBS = {
    "contact":    "contact/contact.db",
    "message":    "message/message_0.db",
    "session":    "session/session.db",
    "head_image": "head_image/head_image.db",
    "hardlink":   "hardlink/hardlink.db",   # 图片md5→本地.dat文件名 映射
    "media":      "message/media_0.db",      # 语音(type=34) 明文 SILK: VoiceInfo
    "msgres":     "message/message_resource.db",  # 视频(type=43) 本地文件基名映射
    "biz_message": "message/biz_message_0.db",   # 公众号/服务号(其他)的消息
}


def find_accounts():
    """返回 [(wxid, db_storage_abs_path), ...]，跳过 all_users/Backup 等非账号目录。"""
    out = []
    if not os.path.isdir(CONTAINER):
        return out
    for name in sorted(os.listdir(CONTAINER)):
        if name in ("all_users", "Backup") or name.startswith("."):
            continue
        dbs = os.path.join(CONTAINER, name, "db_storage")
        if os.path.isdir(dbs):
            out.append((name, dbs))
    return out


def active_account():
    """选取当前账号：优先取 db_storage 下 contact.db 最新 mtime 的那个。"""
    accts = find_accounts()
    if not accts:
        return None, None
    if len(accts) == 1:
        return accts[0]

    def score(item):
        _, dbs = item
        c = os.path.join(dbs, "contact/contact.db")
        return os.path.getmtime(c) if os.path.exists(c) else 0

    return max(accts, key=score)


def db_storage_dir():
    d = _resolve_db_storage()
    if d:
        return d
    _, dbs = active_account()
    return dbs


def _strip_folder_suffix(name):
    # 账号目录名是 <wxid>_<4位hex>，真实 wxid 去掉尾部后缀
    if name:
        return re.sub(r"_[0-9a-f]{4,}$", "", name)
    return name


def wxid():
    d = _resolve_db_storage()
    if d:
        folder = os.path.basename(os.path.dirname(d))
        return _strip_folder_suffix(folder)
    w, _ = active_account()
    return _strip_folder_suffix(w)


# ---------------- 按账号分区的路径 ----------------
def account_key():
    return wxid() or "default"


def account_dir():
    from core import account_session
    d = account_session.bound_root() or os.path.join(ACCOUNTS_DIR, account_key())
    os.makedirs(d, exist_ok=True)
    return d


def keys_json():
    return os.path.join(account_dir(), "keys.json")


def decrypted_dir():
    return os.path.join(account_dir(), "decrypted")


def decrypted_path(key):
    return os.path.join(decrypted_dir(), key + ".db")


def personas_dir():
    return os.path.join(account_dir(), "personas")


def ensure_dirs():
    os.makedirs(decrypted_dir(), exist_ok=True)
    os.makedirs(personas_dir(), exist_ok=True)


# 兼容旧引用
KEYS_JSON = None  # 请用 keys_json()


if __name__ == "__main__":
    print("container:", CONTAINER, "exists:", os.path.isdir(CONTAINER))
    for w, dbs in find_accounts():
        print("account:", w)
        print("  db_storage:", dbs)
    print("active:", active_account()[0])
