#!/usr/bin/env python3
"""容器内发送器：用 xdotool 驱动 Linux 微信发文本/图片。

流程：激活窗口 → 点侧栏搜索 → 粘贴目标名 → 回车打开首个匹配 → 粘贴内容 → 回车发送。
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
    p = sp.Popen(["xclip", "-selection", "clipboard"], stdin=sp.PIPE,
                 env={"DISPLAY": DISPLAY, "PATH": "/usr/bin:/bin"})
    p.communicate(s.encode("utf-8"))


def set_clip_image(path):
    mt = "image/png"
    if path.lower().endswith((".jpg", ".jpeg")):
        mt = "image/jpeg"
    sp.run(["xclip", "-selection", "clipboard", "-t", mt, "-i", path],
           env={"DISPLAY": DISPLAY, "PATH": "/usr/bin:/bin"})


def key(*keys):
    x("xdotool", "key", "--clearmodifiers", *keys)


def open_chat(wid, name):
    x("xdotool", "windowactivate", "--sync", wid)
    time.sleep(0.5)
    px, py, w, h = win_geom(wid)
    # 直接点侧栏搜索框(顶部，会话列表上方)——不用 Ctrl+F(有些版本是"聊天内搜索")
    x("xdotool", "mousemove", str(px + 131), str(py + 43), "click", "1")
    time.sleep(0.35)
    key("ctrl+a")
    time.sleep(0.1)
    key("Delete")            # 清掉搜索框残留
    time.sleep(0.15)
    set_clip_text(name)
    key("ctrl+v")
    time.sleep(1.5)          # 等搜索结果浮出
    key("Down")              # 高亮第一个结果(比裸 Return 更稳)
    time.sleep(0.3)
    key("Return")            # 打开选中的匹配
    time.sleep(1.0)
    return px, py, w, h


def title_shot(out_path):
    """截会话标题区(会话名在顶栏左侧)。返回 (px,py,w,h) 供裁剪，图落 out_path。"""
    wid = win_id()
    if not wid:
        return None
    x("xdotool", "windowactivate", "--sync", wid)
    time.sleep(0.2)
    x("scrot", "-o", out_path)
    return win_geom(wid)


def focus_input(px, py, w, h):
    # 点消息输入区（工具栏下方），确保输入框获得焦点
    x("xdotool", "mousemove", str(px + w // 2), str(py + h - 70), "click", "1")
    time.sleep(0.3)


def send_text(name, text):
    wid = win_id()
    if not wid:
        print("ERR:no-window"); sys.exit(2)
    px, py, w, h = open_chat(wid, name)
    focus_input(px, py, w, h)
    set_clip_text(text)
    time.sleep(0.2)
    key("ctrl+v")
    time.sleep(0.5)
    key("Return")
    print("OK")


def just_open(name):
    """只打开会话、不发送(供 host 端截图核对标题后再发)。"""
    wid = win_id()
    if not wid:
        print("ERR:no-window"); sys.exit(2)
    open_chat(wid, name)
    print("OK")


def search_query(name):
    """只在侧栏搜索框输入查询、弹出结果下拉，【不回车】——供 host 端视觉定位结果行后点击。"""
    wid = win_id()
    if not wid:
        print("ERR:no-window"); sys.exit(2)
    x("xdotool", "windowactivate", "--sync", wid)
    time.sleep(0.4)
    px, py, _w, _h = win_geom(wid)
    x("xdotool", "mousemove", str(px + 131), str(py + 43), "click", "1")
    time.sleep(0.35)
    key("ctrl+a"); time.sleep(0.1); key("Delete"); time.sleep(0.15)
    set_clip_text(name)          # 剪贴板粘贴，避免 xdotool type 丢首字符
    key("ctrl+v")
    time.sleep(1.6)              # 等结果下拉渲染
    print("OK")


def click_xy(cx, cy):
    x("xdotool", "mousemove", str(cx), str(cy), "click", "1")
    time.sleep(1.0)
    print("OK")


def paste_text(text):
    """向【当前已打开】的会话发文本(不重新搜索/打开)。"""
    wid = win_id()
    if not wid:
        print("ERR:no-window"); sys.exit(2)
    px, py, w, h = win_geom(wid)
    focus_input(px, py, w, h)
    set_clip_text(text)
    time.sleep(0.2)
    key("ctrl+v")
    time.sleep(0.5)
    key("Return")
    print("OK")


def paste_at(member_name, text):
    """向【当前已打开的群】发一条【@某人】的消息(不重新搜索)。
    流程：聚焦输入框→按@弹出成员选择器→粘成员名过滤→回车选中(插入真·@提及)→粘正文→发送。"""
    wid = win_id()
    if not wid:
        print("ERR:no-window"); sys.exit(2)
    px, py, w, h = win_geom(wid)
    focus_input(px, py, w, h)
    key("at")                     # 按 @ 触发成员选择器
    time.sleep(0.9)
    set_clip_text(member_name)     # 粘成员名做过滤
    key("ctrl+v")
    time.sleep(1.1)
    key("Return")                  # 选中高亮项 → 插入真正的 @提及 + 空格
    time.sleep(0.5)
    set_clip_text(text)            # 粘正文
    key("ctrl+v")
    time.sleep(0.5)
    key("Return")                  # 发送
    print("OK")


def paste_image(path):
    """向【当前已打开】的会话发图片(不重新搜索/打开)。"""
    wid = win_id()
    if not wid:
        print("ERR:no-window"); sys.exit(2)
    px, py, w, h = win_geom(wid)
    x("xdotool", "mousemove", str(px + 397), str(py + h - 131), "click", "1")
    time.sleep(1.5)
    key("ctrl+a"); time.sleep(0.2); key("Delete"); time.sleep(0.2)
    x("xdotool", "type", "--clearmodifiers", "--delay", "25", path)
    time.sleep(0.4)
    key("Return"); time.sleep(1.5)
    key("Return")
    print("OK")


def send_image(name, path):
    wid = win_id()
    if not wid:
        print("ERR:no-window"); sys.exit(2)
    px, py, w, h = open_chat(wid, name)
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


def open_and_scroll(name, rounds=6):
    """打开会话并向上滚动，促使微信渲染(解密)历史图片到 temp/ImageUtils。"""
    wid = win_id()
    if not wid:
        print("ERR:no-window"); sys.exit(2)
    px, py, w, h = open_chat(wid, name)
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
    if kind == "open":
        open_and_scroll(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 6)
    elif kind == "justopen":          # 只打开会话，不发送
        just_open(sys.argv[2])
    elif kind == "searchquery":       # 只输入搜索、弹结果，不回车(供视觉定位)
        search_query(sys.argv[2])
    elif kind == "clickxy":           # 点击绝对坐标 x y
        click_xy(int(sys.argv[2]), int(sys.argv[3]))
    elif kind == "titleshot":         # 截当前窗口(含会话标题)到指定路径
        title_shot(sys.argv[2]); print("OK")
    elif kind == "pastetext":         # 向当前已打开会话发文本
        paste_text(sys.argv[2])
    elif kind == "pasteat":           # 向当前已打开群发 @某人 的消息: pasteat <成员名> <正文>
        paste_at(sys.argv[2], sys.argv[3])
    elif kind == "pasteimage":        # 向当前已打开会话发图片
        paste_image(sys.argv[2])
    elif len(sys.argv) < 4:
        print(__doc__); sys.exit(1)
    elif kind == "text":
        send_text(sys.argv[2], sys.argv[3])
    elif kind == "image":
        send_image(sys.argv[2], sys.argv[3])
    else:
        print("kind must be text|image|open|justopen|pastetext|pasteimage|titleshot")
        sys.exit(1)
