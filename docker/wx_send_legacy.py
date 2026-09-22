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


def search_box_holds(name):
    """读当前焦点框。搜索没聚焦时这里读到的是输入框，不能当已搜到人。"""
    sentinel = "WXBOT_SEARCH_SENTINEL"
    old = clipget()
    try:
        set_clip_text(sentinel)
        key("ctrl+a")
        time.sleep(0.08)
        key("ctrl+c")
        time.sleep(0.15)
        got = clipget()
        key("Right")
        time.sleep(0.05)
        return got == name
    finally:
        set_clip_text(old)


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
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.7


def searched_by_wechat_id(name):
    """可搜索的微信号是全局唯一的 ASCII；中文备注/带空格的显示名走标题核对。"""
    s = (name or "").strip()
    return bool(s) and " " not in s and "@" not in s and all(ord(c) < 128 for c in s)


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


def open_chat(wid, name, expect=""):
    close_overlays()
    px, py, w, h = ensure_focused(wid)
    # 关掉残留搜索浮层；不要在输入框里回车。
    key("Escape")
    time.sleep(0.12)
    key("Escape")
    time.sleep(0.12)
    px, py, w, h = win_geom(wid)
    # 连点两次：第一下落在未聚焦窗口上时只激活，第二次才进搜索框。
    click_search(px, py)
    click_search(px, py)
    key("ctrl+a")
    time.sleep(0.08)
    key("Delete")
    time.sleep(0.12)
    set_clip_text(name)
    key("ctrl+v")
    time.sleep(0.35)
    if not search_box_holds(name):
        key("Escape")
        print("ERR:search-unfocused"); sys.exit(3)
    time.sleep(1.3)          # 等搜索结果
    if searched_by_wechat_id(name):
        # 下拉还在时双击「联系人」第一条。单击只出资料卡；不要 Down，会落到同微信号的群。
        click_search_hit(px, py, 128, clicks=2)
        time.sleep(0.8)
        key("Escape")
        time.sleep(0.15)
        px, py, w, h = win_geom(wid)
        if not pane_has_header(header_crop_path(px, py, w, h)):
            print("ERR:chat-not-opened"); sys.exit(3)
        return px, py, w, h
    # 群名/备注：联系人第一条已经高亮时回车打开的是这一行。
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


def send_text(name, text, expect=""):
    wid = win_id()
    if not wid:
        print("ERR:no-window"); sys.exit(2)
    px, py, w, h = open_chat(wid, name, expect)
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
    wid = win_id()
    if not wid:
        print("ERR:no-window"); sys.exit(2)
    px, py, w, h = open_chat(wid, name, expect)
    # 点输入区「发送文件」文件夹图标 → GTK 文件选择器 → 输入路径 → 打开 → 回车发送
    x("xdotool", "mousemove", str(px + 397), str(py + h - 131), "click", "1")
    time.sleep(1.5)          # 等文件选择器
    key("ctrl+a")
    time.sleep(0.2)
    key("Delete")            # 清空文件名框
    time.sleep(0.2)
    # 逐字符输入路径（--delay 防 GTK 丢字符），不走剪贴板避免与会话名混淆
    x("xdotool", "type", "--clearmodifiers", "--delay", "25", path)
    time.sleep(0.4)
    key("Return")            # 打开文件 → 图片进入输入框
    time.sleep(1.5)
    key("Return")            # 发送
    print("OK")


def just_open(name, expect=""):
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
