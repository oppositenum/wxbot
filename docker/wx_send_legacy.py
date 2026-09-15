#!/usr/bin/env python3
"""容器内发送器：用 xdotool 驱动 Linux 微信发文本/图片。

流程：激活窗口 → 点侧栏搜索 → 粘贴目标名 → 点击首条结果打开会话
→ 点输入框并清空 → 粘贴正文 → 回车/点发送。
不要在搜索后对输入框按回车：微信号会当成一条消息发出去。
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


def close_overlays():
    """朋友圈/图片预览会盖在聊天上,人手和脚本都点不到输入框。"""
    for title in ("^朋友圈$", "^图片$", "^视频$"):
        r = x("xdotool", "search", "--name", title)
        for oid in [l for l in r.stdout.split() if l.strip()]:
            x("xdotool", "windowactivate", "--sync", oid)
            key("Escape")
            time.sleep(0.15)
            x("xdotool", "windowclose", oid)


def open_chat(wid, name):
    close_overlays()
    x("xdotool", "windowactivate", "--sync", wid)
    time.sleep(0.4)
    px, py, w, h = win_geom(wid)
    # 点侧栏搜索框。不要 Ctrl+F: Linux 微信那是聊天内搜索。
    # 不要在这里按回车：搜索框没焦点时，回车会把微信号从输入框发出去。
    x("xdotool", "mousemove", str(px + 131), str(py + 43), "click", "1")
    time.sleep(0.35)
    key("ctrl+a")
    time.sleep(0.1)
    key("Delete")
    time.sleep(0.15)
    set_clip_text(name)
    key("ctrl+v")
    time.sleep(1.6)          # 等搜索结果
    # 点侧栏第一条结果打开会话（搜索框下方）。
    x("xdotool", "mousemove", str(px + 170), str(py + 108), "click", "1")
    time.sleep(1.0)
    # 点消息区，收起搜索浮层，避免后续按键还打在搜索框。
    x("xdotool", "mousemove", str(px + w // 2), str(py + h // 2), "click", "1")
    time.sleep(0.25)
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


def send_text(name, text):
    wid = win_id()
    if not wid:
        print("ERR:no-window"); sys.exit(2)
    px, py, w, h = open_chat(wid, name)
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
    elif len(sys.argv) < 4:
        print(__doc__); sys.exit(1)
    elif kind == "text":
        send_text(sys.argv[2], sys.argv[3])
    elif kind == "image":
        send_image(sys.argv[2], sys.argv[3])
    else:
        print("kind must be text|image|open"); sys.exit(1)
