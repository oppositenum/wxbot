#!/usr/bin/env python3
"""容器内发送器：用 xdotool 驱动 Linux 微信发文本/图片。

流程：真正抢回主窗口焦点 → 关掉聊天内搜索等浮层 → 点侧栏搜索
→ 确认搜索框吃到了查询词。
私聊优先搜唯一微信号：结果里「联系人」第一条就是这个人，点开即可发，
不再用备注/昵称做标题 OCR。群名或没有微信号时，仍用回车 + 标题核对。
搜索没焦点、或会话没打开时绝不往下发。
不要在搜索框没焦点时回车：微信号会当成一条消息发出去。
用法：
  python3 wx_send.py text  "文件传输助手" "你好"
  python3 wx_send.py image "文件传输助手" /path/in/container.png
"""
import subprocess as sp
import sys
import time

DISPLAY = ":0"


def x(*args, **kw):
    return sp.run(args, env={"DISPLAY": DISPLAY, "PATH": "/usr/bin:/bin"},
                  capture_output=True, text=True, **kw)


def win_id():
    r = x("xdotool", "search", "--name", "^微信$")
    ids = [l for l in r.stdout.split() if l.strip()]
    return ids[-1] if ids else None


def detached_chat_id(expect):
    """独立聊天窗标题等于会话名（自己跟自己）。子串匹配会误伤别的窗口，必须整标题相等。"""
    if not expect:
        return None
    r = x("xdotool", "search", "--name", "^" + expect + "$")
    ids = [l for l in r.stdout.split() if l.strip()]
    main = set()
    wr = x("xdotool", "search", "--name", "^微信$")
    main.update(l for l in wr.stdout.split() if l.strip())
    for i in reversed(ids):
        name = x("xdotool", "getwindowname", i).stdout.strip()
        if i not in main and name == expect:
            return i
    return None


def win_geom(wid):
    r = x("xdotool", "getwindowgeometry", wid)
    px = py = w = h = 0
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("Position:"):
            px, py = (int(v) for v in line.split()[1].split(","))
        elif line.startswith("Geometry:"):
            w, h = (int(v) for v in line.split()[1].split("x"))
    return px, py, w, h


def set_clip_text(s):
    p = sp.Popen(["xclip", "-selection", "clipboard"], stdin=sp.PIPE, stdout=sp.DEVNULL, stderr=sp.DEVNULL,
                 env={"DISPLAY": DISPLAY, "PATH": "/usr/bin:/bin"})
    p.communicate(s.encode("utf-8"))


def set_clip_image(path):
    mt = "image/png"
    if path.lower().endswith((".jpg", ".jpeg")):
        mt = "image/jpeg"
    sp.run(["xclip", "-selection", "clipboard", "-t", mt, "-i", path], stdout=sp.DEVNULL, stderr=sp.DEVNULL,
           env={"DISPLAY": DISPLAY, "PATH": "/usr/bin:/bin"})


def key(*keys):
    x("xdotool", "key", "--clearmodifiers", *keys)


def clipget():
    r = x("xclip", "-selection", "clipboard", "-o", "-target", "UTF8_STRING")
    return r.stdout if r.returncode == 0 else ""


def close_overlays():
    """朋友圈/图片预览/聊天内搜索会盖住主窗口，按键会打到浮层或当前会话。"""
    for title in ("^朋友圈$", "^图片$", "^视频$", "^搜索聊天记录$"):
        r = x("xdotool", "search", "--name", title)
        for oid in [l for l in r.stdout.split() if l.strip()]:
            x("xdotool", "windowactivate", "--sync", oid)
            key("Escape")
            time.sleep(0.15)
            x("xdotool", "windowclose", oid)


def ensure_focused(wid):
    """windowactivate 在 XFCE 里经常只点亮窗口、键盘焦点还在别处。"""
    px, py, w, h = win_geom(wid)
    for _ in range(3):
        x("xdotool", "windowactivate", "--sync", wid)
        x("xdotool", "windowraise", wid)
        x("xdotool", "windowfocus", "--sync", wid)
        time.sleep(0.15)
        # 点标题栏：第一下专门用来抢焦点，避免随后点搜索被当成“激活窗口”。
        x("xdotool", "mousemove", str(px + max(w // 2, 400)), str(py + 12), "click", "1")
        time.sleep(0.2)
        if x("xdotool", "getactivewindow").stdout.strip() == str(wid):
            return win_geom(wid)
    print("ERR:no-focus"); sys.exit(3)


def click_search(px, py):
    # 侧栏顶部搜索框。不要 Ctrl+F：Linux 微信那是“搜索聊天记录”。
    x("xdotool", "mousemove", str(px + 131), str(py + 43), "click", "1")
    time.sleep(0.25)


def focused_text():
    """当前焦点框全文。搜索没聚焦时读到的是对话框，不能当已搜到人。"""
    old = clipget()
    try:
        key("ctrl+a")
        time.sleep(0.08)
        key("ctrl+c")
        time.sleep(0.15)
        return clipget()
    finally:
        set_clip_text(old)


def abort_if_composer_polluted():
    """焦点若在对话框：清掉刚贴进去的搜索词，绝不回车发送。"""
    key("ctrl+a")
    time.sleep(0.05)
    key("Delete")
    time.sleep(0.08)
    key("Escape")


def focus_search_box(px, py):
    """点一次侧栏搜索，立刻准备贴查询词。不要再读剪贴板判断空框：浮层打开后
    Ctrl+C 经常读到对话框旧内容，脚本会误判然后停住。"""
    click_search(px, py)
    time.sleep(0.25)
    key("ctrl+a")
    time.sleep(0.05)
    key("Delete")
    time.sleep(0.08)
    return True


def search_box_holds(name):
    return focused_text() == name


def click_search_hit(px, py, dy, clicks=1):
    # 侧栏搜索「联系人」第一条。108 是分组标题；再往下是「群聊」（同微信号的群）。
    # 单击只出资料预览，双击才打开会话。
    cmd = ["xdotool", "mousemove", str(px + 170), str(py + dy), "click", "--repeat", str(clicks), "--delay", "80", "1"]
    x(*cmd)
    time.sleep(0.35)


def header_text(px, py, w, h):
    """读会话顶栏标题。OCR 只做核对，不拿来选人。"""
    full = "/tmp/wx_header_full.png"
    crop = "/tmp/wx_header_crop.png"
    x("scrot", "-o", full)
    try:
        from PIL import Image
        im = Image.open(full)
        im.crop((px + 286, py + 6, px + min(w - 80, 700), py + 52)).save(crop)
    except Exception:
        return ""
    r = x("tesseract", crop, "stdout", "-l", "chi_sim+eng", "--psm", "7")
    return "".join((r.stdout or "").split())


def header_matches(got, expect):
    if not expect:
        return True
    a = "".join((got or "").split())
    b = "".join((expect or "").split())
    if not a or not b:
        return False
    if b in a or a in b:
        return True
    import difflib
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio >= 0.7


def searched_by_wechat_id(name):
    """可搜索的微信号：ASCII、无空格。纯英文短名（Limit）是群名/备注，走标题核对。"""
    s = (name or "").strip()
    if not s or " " in s or "@" in s or any(ord(c) >= 128 for c in s):
        return False
    if len(s) < 6:
        return False
    return any(c.isdigit() for c in s) or "_" in s


def header_crop_path(px, py, w, h):
    full = "/tmp/wx_header_full.png"
    crop = "/tmp/wx_header_crop.png"
    x("scrot", "-o", full)
    try:
        from PIL import Image
        im = Image.open(full)
        im.crop((px + 286, py + 6, px + min(w - 80, 700), py + 52)).save(crop)
        return crop
    except Exception:
        return ""


def pane_has_header(path):
    """空会话占位是一片浅色；真正打开后顶栏有字或头像。"""
    if not path:
        return False
    try:
        from PIL import Image, ImageStat
        return ImageStat.Stat(Image.open(path).convert("L")).stddev[0] >= 8
    except Exception:
        return False


def already_on_chat(px, py, w, h, expect):
    """当前顶栏已经是目标会话：不再搜，避免把搜索词打进输入框。"""
    if not expect:
        return False
    return header_matches(header_text(px, py, w, h), expect)


def open_chat(wid, name, expect=""):
    close_overlays()
    px, py, w, h = ensure_focused(wid)
    # 关掉残留搜索浮层；不要在输入框里回车。
    key("Escape")
    time.sleep(0.12)
    key("Escape")
    time.sleep(0.12)
    px, py, w, h = win_geom(wid)
    if already_on_chat(px, py, w, h, expect):
        return px, py, w, h
    focus_search_box(px, py)
    set_clip_text(name)
    time.sleep(0.08)
    key("ctrl+v")
    time.sleep(0.9)          # 等搜索结果
    before = header_text(px, py, w, h)
    if searched_by_wechat_id(name):
        click_search_hit(px, py, 128, clicks=2)
        time.sleep(0.8)
        key("Escape")
        time.sleep(0.15)
        px, py, w, h = win_geom(wid)
        if not pane_has_header(header_crop_path(px, py, w, h)):
            print("ERR:chat-not-opened"); sys.exit(3)
        return px, py, w, h
    # 群名/备注/文件传输助手：联系人第一条已经高亮时回车打开的是这一行。
    key("Return")
    time.sleep(1.0)
    # Do not click the search box again to test whether navigation succeeded:
    # that click steals focus and can reopen a search/preview layer. Escape is
    # keyboard-only cleanup; the title check below is the send safety gate.
    key("Escape")
    time.sleep(0.15)
    px, py, w, h = win_geom(wid)
    if expect and not header_matches(header_text(px, py, w, h), expect):
        # One bounded fallback selects the visible contact row without ever
        # clicking the message area or the search input again.
        click_search_hit(px, py, 128)
        time.sleep(0.8)
        key("Escape")
        time.sleep(0.15)
        px, py, w, h = win_geom(wid)
        if not header_matches(header_text(px, py, w, h), expect):
            print("ERR:wrong-chat"); sys.exit(3)
    return px, py, w, h


def focus_input(px, py, w, h):
    # 点消息输入区（工具栏下方），确保输入框获得焦点
    x("xdotool", "mousemove", str(px + w // 2), str(py + h - 70), "click", "1")
    time.sleep(0.3)


def clear_input():
    key("ctrl+a")
    time.sleep(0.08)
    key("Delete")
    time.sleep(0.12)


def click_send(px, py, w, h):
    # 输入框右下角「发送」按钮。只按回车时,焦点若还在搜索框/资料卡,字贴进去也不会发。
    x("xdotool", "mousemove", str(px + w - 70), str(py + h - 36), "click", "1")
    time.sleep(0.3)


def activate_chat_window(name, expect=""):
    """独立聊天窗已打开就用它，避免主窗口（老婆）被 open_chat 搜开。"""
    det = detached_chat_id(expect)
    if det:
        px, py, w, h = win_geom(det)
        x("xdotool", "windowactivate", "--sync", det)
        x("xdotool", "windowraise", det)
        x("xdotool", "windowfocus", "--sync", det)
        time.sleep(0.2)
        return px, py, w, h
    wid = win_id()
    if not wid:
        print("ERR:no-window"); sys.exit(2)
    return open_chat(wid, name, expect)


def send_text(name, text, expect=""):
    px, py, w, h = activate_chat_window(name, expect)
    focus_input(px, py, w, h)
    clear_input()            # 搜人用的微信号若漏进输入框，先清掉再贴正文
    set_clip_text(text)
    time.sleep(0.2)
    key("ctrl+v")
    time.sleep(0.45)
    key("Return")
    time.sleep(0.25)
    click_send(px, py, w, h)
    print("OK")


def send_image(name, path, expect=""):
    px, py, w, h = activate_chat_window(name, expect)
    focus_input(px, py, w, h)
    clear_input()
    set_clip_image(path)
    time.sleep(0.25)
    key("ctrl+v")
    time.sleep(0.8)
    key("Return")
    time.sleep(0.25)
    click_send(px, py, w, h)
    print("OK")


def just_open(name, expect=""):
    det = detached_chat_id(expect)
    if det:
        x("xdotool", "windowactivate", "--sync", det)
        x("xdotool", "windowraise", det)
        print("OK")
        return
    wid = win_id()
    if not wid:
        print("ERR:no-window"); sys.exit(2)
    open_chat(wid, name, expect)
    print("OK")


def open_and_scroll(name, rounds=6, expect=""):
    """打开会话并向上滚动，促使微信渲染(解密)历史图片到 temp/ImageUtils。"""
    wid = win_id()
    if not wid:
        print("ERR:no-window"); sys.exit(2)
    px, py, w, h = open_chat(wid, name, expect)
    # 鼠标移到消息区中间，滚轮上滚(button 4)，逐步加载并渲染历史图片
    x("xdotool", "mousemove", str(px + w // 2), str(py + h // 2))
    time.sleep(0.3)
    for _ in range(int(rounds)):
        for _ in range(5):
            x("xdotool", "click", "4")      # 滚轮上滚
            time.sleep(0.15)
        time.sleep(0.7)                      # 等渲染/解密落盘
    print("OK")


if __name__ == "__main__":
    kind = sys.argv[1] if len(sys.argv) > 1 else ""
    expect = sys.argv[4] if len(sys.argv) > 4 else ""
    if kind == "justopen":
        just_open(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    elif kind == "open":
        open_and_scroll(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 6, expect)
    elif len(sys.argv) < 4:
        print(__doc__); sys.exit(1)
    elif kind == "text":
        send_text(sys.argv[2], sys.argv[3], expect)
    elif kind == "image":
        send_image(sys.argv[2], sys.argv[3], expect)
    else:
        print("kind must be text|image|open|justopen"); sys.exit(1)
