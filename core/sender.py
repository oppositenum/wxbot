"""可靠发送层：在 docker_wx 原始 xdotool 发送之上，加"发后校验 + 重试"。

核心保证：发完后重新解密 message 库，确认目标会话里真的多出一条【自己发的、
内容匹配的】新消息。若没出现(发错会话/被吞/UI 抖动)——自动重试。这比截图核对
标题更硬：直接以"消息确实落到了正确会话的库里"为成功判据，天然排除发错会话。

需要 chat_username(会话 wxid)才能校验；只有显示名时退回不校验的原始发送。

(注：早期 macOS-native PyObjC 发送器已废弃，架构改为 Docker 内 xdotool。)
"""
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402,F401
from core import docker_wx  # noqa: E402

VERIFY_WINDOW = 10.0       # 发后最多等这么多秒确认消息落库(宽一点，避免误判失败→重发变刷屏)
POLL_STEP = 0.6
_TITLE_TMP = "/tmp/wxbot_title.png"

# 整条发送(打开→核对→发送→校验)的粗粒度锁：UI_LOCK 只锁单步操作，两条发送会在步骤间
# 相互穿插(A 刚打开会话、B 又打开别的会话→A 发错人)。这把锁保证【一整条发送】原子，
# 网页队列worker 与 机器人线程 的发送彼此串行，绝不交错。
SEND_LOCK = threading.Lock()


def _norm_name(s):
    """会话名归一：去空白、去群成员数后缀"(60)"、去零宽字符。"""
    s = re.sub(r"[（(]\s*\d+\s*[)）]\s*$", "", s or "")
    return re.sub(r"\s| |​", "", s)


def _title_matches(target, title):
    a, b = _norm_name(target), _norm_name(title)
    if not a or not b:
        return False
    return a in b or b in a or (len(a) >= 4 and len(b) >= 4 and a[:4] == b[:4])


def _read_open_title():
    """截图 + 视觉读取【当前打开会话】的标题；视觉不可用返回 None(表示无法核对)。"""
    from core import llm
    if not llm.available():
        return None
    try:
        if not docker_wx.title_shot(_TITLE_TMP):
            return None
        data = open(_TITLE_TMP, "rb").read()
        return llm.describe_image(
            data, media_type="image/png",           # scrot 出的是 PNG，别按 jpeg 发
            prompt="这是微信窗口截图。只返回【右侧对话区顶部】的会话标题(联系人名或群名称)，"
            "不要引号、不要解释、不要其它任何字；若看不到就返回 NONE。")
    except Exception:  # noqa: BLE001
        return None


LIST_X = 340               # 会话列表/搜索结果下拉所在列的中心 x(绝对像素)


def search_key(chat_username, display_name):
    """把"要发给谁"解析成 (搜索词, 结果行显示名)。

    重名克星：remark 可能多个联系人重名(如 6 个"老婆")，搜它会出多行、开错人。
    而【微信号 alias 全局唯一且可被搜索框命中】，故优先按 wxid 反查出 alias 去搜，
    把结果收敛到唯一一行；结果行显示的仍是 remark/昵称，故用 display_name 定位点击。
    群聊无此问题，直接用群名。alias 缺失时退回按显示名搜。
    """
    if not chat_username or chat_username.endswith("@chatroom"):
        return display_name, display_name
    try:
        from core import db
        con = db.connect("contact")
        try:
            r = con.execute("SELECT alias, remark, nick_name FROM contact "
                            "WHERE username=?", (chat_username,)).fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return display_name, display_name
    if r:
        alias = (r["alias"] or "").strip()
        locate = display_name or (r["remark"] or "").strip() or (r["nick_name"] or "").strip()
        if alias and not alias.startswith("wxid_"):
            return alias, (locate or alias)         # 搜唯一微信号，按显示名定位行
    return display_name, display_name


def _vision_open(display_name, chat_username=None):
    """视觉引导打开会话：搜索框输入(唯一)搜索词→截图→视觉定位结果行 y→点击→核对标题。

    比"盲按 Down+Return"可靠得多：微信搜索下拉把"网络结果/搜一搜"排在前面，
    盲选会开错；视觉直接定位【显示名匹配】的会话/联系人/群/功能那一行再点。
    搜索词优先用唯一微信号(见 search_key)以消除重名。视觉不可用则退回原始打开。
    """
    from core import llm
    query, locate_name = search_key(chat_username, display_name)
    if not llm.available():
        return docker_wx.open_chat(query)
    locate = ("这是微信桌面截图，左侧搜索框下方弹出了搜索结果下拉。"
              f"找到名称正好是\"{locate_name}\"的那一行(优先【联系人/群聊/功能】分组下，"
              "不要选【搜索网络结果/搜一搜】那种)，只输出该行中心的像素 y 坐标(纯整数)；"
              "找不到就输出 -1。只输出一个数字。")
    for _attempt in range(3):
        if not docker_wx.search_query(query):
            continue
        try:
            if not docker_wx.title_shot(_TITLE_TMP):
                continue
            data = open(_TITLE_TMP, "rb").read()
            raw = llm.describe_image(data, media_type="image/png", prompt=locate) or ""
            m = re.search(r"-?\d+", raw)
            y = int(m.group()) if m else -1
        except Exception:  # noqa: BLE001
            y = -1
        if y > 120:
            docker_wx.click(LIST_X, y)
            time.sleep(1.2)
            if chat_username:
                docker_wx.note_open(chat_username)     # 记住当前打开的会话
            return True
        time.sleep(0.4)
    # 视觉不可用/失败(如 LLM 中转挂了)→退回搜索式打开(search+回车)，不至于完全发不出
    ok = docker_wx.open_chat(query)
    if ok and chat_username:
        docker_wx.note_open(chat_username)
    return ok


_focus_lock = threading.Lock()


def focus_chat(display_name, chat_username):
    """保持"监控会话"在微信里打开：图一到微信就自动下清晰版(_b.dat)，秒撤也来得及。
    若已经开在该会话就跳过(微信会一直停在这直到发送/翻图切走)。"""
    if not chat_username:
        return False
    with _focus_lock:
        if docker_wx.current_open() == chat_username:
            return True                               # 已开在这，无需再动
        ok = _vision_open(display_name, chat_username)
        return ok


def _latest_self_id(chat_username):
    """当前该会话里"自己发的消息"的最大 local_id（作为发送前基线）。"""
    from core import messages
    try:
        msgs = messages.get_messages(chat_username, limit=30)
    except Exception:  # noqa: BLE001
        return -1
    ids = [m["local_id"] for m in msgs if m.get("is_self") and m.get("local_id")]
    return max(ids) if ids else -1


def _norm(s):
    return "".join((s or "").split())


def _verify_text(chat_username, text, baseline_id, deadline):
    """轮询解密库，确认出现一条 is_self、内容匹配、local_id>baseline 的新消息。"""
    from core import decrypt, messages
    want = _norm(text)
    while time.time() < deadline:
        time.sleep(POLL_STEP)
        try:
            decrypt.run(force=True, only=["message"])
            msgs = messages.get_messages(chat_username, limit=30)
        except Exception:  # noqa: BLE001
            continue
        for m in msgs:
            if (m.get("is_self") and (m.get("local_id") or -1) > baseline_id
                    and m.get("type") == 1 and _norm(m.get("content")) == want):
                return True
    return False


def _delivered_text(chat_username, text, baseline_id):
    """一次性检查(不轮询)：目标文本是否已作为自己发的新消息出现——用于重发前防重复。"""
    from core import decrypt, messages
    want = _norm(text)
    try:
        decrypt.run(force=True, only=["message"])
        for m in messages.get_messages(chat_username, limit=30):
            if (m.get("is_self") and (m.get("local_id") or -1) > baseline_id
                    and m.get("type") == 1 and _norm(m.get("content")) == want):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _verify_image(chat_username, baseline_id, deadline):
    """图片没法比内容，只确认出现一条 is_self 的新图片消息(type=3)。"""
    from core import decrypt, messages
    while time.time() < deadline:
        time.sleep(POLL_STEP)
        try:
            decrypt.run(force=True, only=["message"])
            msgs = messages.get_messages(chat_username, limit=30)
        except Exception:  # noqa: BLE001
            continue
        for m in msgs:
            if (m.get("is_self") and (m.get("local_id") or -1) > baseline_id
                    and m.get("type") == 3):
                return True
    return False


def _guarded_send(display_name, chat_username, do_paste, verify_fn, retries,
                  predelivered=None):
    """打开会话 → (能核对就)截图核对标题 → 发送 → 发后校验落库。

    关键防误发：能视觉核对标题时，标题不匹配【绝不发送】(避免发错会话被刷屏)，
    只重开重试。视觉不可用时退回"原子打开+发送"，靠发后落库校验兜底(不会误报成功)。
    防重发：baseline 只取一次；重试前先看"是不是上一次其实已经发出去了"(predelivered)，
    是就直接判成功，绝不重复发。
    """
    baseline = _latest_self_id(chat_username)        # 只取一次，作为全程基线
    last = {"ok": False, "error": "未发送"}
    for attempt in range(1, retries + 2):
        # 重试前先核对：上一次可能其实已送达(只是当时校验窗口没等到)——避免重复发
        if attempt > 1 and predelivered and predelivered(baseline):
            return {"ok": True, "verified": True, "attempts": attempt - 1,
                    "note": "已送达(上次)"}
        if not _vision_open(display_name, chat_username):
            last = {"ok": False, "error": "打开会话失败", "attempts": attempt}
            time.sleep(0.8)
            continue
        title = _read_open_title()                   # None=视觉不可用，无法核对
        if title and title.upper() != "NONE" and not _title_matches(display_name, title):
            last = {"ok": False, "error": f"打开的会话不对(标题={title!r})，已阻止发送",
                    "attempts": attempt, "wrong_chat": True}
            time.sleep(0.6)
            continue                                 # 重开重试，绝不发到错会话
        raw = do_paste()                             # 向当前已打开会话发送
        if not raw.get("ok"):
            last = raw
            time.sleep(0.8)
            continue
        if verify_fn(baseline):
            return {"ok": True, "verified": True, "attempts": attempt,
                    "title_checked": bool(title)}
        last = {"ok": False, "error": "发送未确认(库中未见)", "attempts": attempt}
        time.sleep(0.8)
    last.setdefault("verified", False)
    return last


def send_text(display_name, text, chat_username=None, retries=2, verify=True):
    """发文本：打开会话→核对标题(防误发)→发送→发后落库校验+重试。

    有 chat_username 时逐次校验、失败重试；无则退回原始发送(ok 依赖 xdotool 回显)。
    发送享有 UI 优先级：让正在翻图解密(harvest)的后台活立刻让出微信窗口。
    """
    docker_wx.request_priority()             # 抢占：harvest 会立刻让位
    try:
        with SEND_LOCK:                      # 整条发送原子，绝不与另一条发送交错
            if not (verify and chat_username):
                r = docker_wx.send_text(display_name, text)
                r.setdefault("verified", False)
                return r
            return _guarded_send(
                display_name, chat_username,
                do_paste=lambda: docker_wx.paste_text(text),
                verify_fn=lambda base: _verify_text(chat_username, text, base,
                                                    time.time() + VERIFY_WINDOW),
                retries=retries,
                predelivered=lambda base: _delivered_text(chat_username, text, base))
    finally:
        docker_wx.release_priority()


def _verify_contains(chat_username, text, baseline_id, deadline):
    """确认出现一条 is_self、内容【包含】text 的新消息(用于 @提及：内容是 '@某人 正文')。"""
    from core import decrypt, messages
    want = _norm(text)
    while time.time() < deadline:
        time.sleep(POLL_STEP)
        try:
            decrypt.run(force=True, only=["message"])
            msgs = messages.get_messages(chat_username, limit=30)
        except Exception:  # noqa: BLE001
            continue
        for m in msgs:
            if (m.get("is_self") and (m.get("local_id") or -1) > baseline_id
                    and m.get("type") == 1 and want and want in _norm(m.get("content"))):
                return True
    return False


def send_at(display_name, chat_username, member_wxid, member_display, text, retries=2):
    """向群里发一条【@某人】的消息。member_display=群里显示名(用于@选择器过滤)。"""
    docker_wx.request_priority()
    try:
        with SEND_LOCK:
            return _guarded_send(
                display_name, chat_username,
                do_paste=lambda: docker_wx.paste_at(member_display or "", text),
                verify_fn=lambda base: _verify_contains(chat_username, text, base,
                                                        time.time() + VERIFY_WINDOW),
                retries=retries,
                predelivered=lambda base: _verify_contains(chat_username, text, base,
                                                           time.time() + 0.1))
    finally:
        docker_wx.release_priority()


def send_image(display_name, host_path, chat_username=None, retries=1, verify=True):
    """发图片：打开会话→核对标题(防误发)→发送→发后校验(出现新 is_self 图片)+重试。"""
    if not os.path.exists(host_path):
        return {"ok": False, "error": f"图片不存在: {host_path}", "verified": False}
    docker_wx.request_priority()
    try:
        with SEND_LOCK:
            if not (verify and chat_username):
                r = docker_wx.send_image(display_name, host_path)
                r.setdefault("verified", False)
                return r
            return _guarded_send(
                display_name, chat_username,
                do_paste=lambda: docker_wx.paste_image_open(host_path),
                verify_fn=lambda base: _verify_image(chat_username, base,
                                                     time.time() + VERIFY_WINDOW),
                retries=retries)
    finally:
        docker_wx.release_priority()
