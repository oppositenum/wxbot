#!/usr/bin/env python3
"""Container navigation helpers. Direct sending is deliberately blocked.
The current xdotool interface cannot prove stable recipient/account identity.
Use core.sender for a durable cannot_confirm_target result; names are insufficient.
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
    time.sleep(1.8)          # 等搜索结果浮出(精确微信号→下拉首行即目标联系人)
    # 像人手动那样:输入完直接回车打开首个匹配。不按 Down——多按 Down 会把高亮从
    # "首个精确联系人"移到"搜一搜网络结果/聊天记录",甚至结果没弹出时落到会话列表(误开)。
    key("Return")
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
    raise RuntimeError("cannot_confirm_target: direct UI sending disabled")


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
    raise RuntimeError("cannot_confirm_target: direct UI sending disabled")


def paste_at(member_name, text):
    raise RuntimeError("cannot_confirm_target: direct UI sending disabled")


def paste_image(path):
    raise RuntimeError("cannot_confirm_target: direct UI sending disabled")


def send_image(name, path):
    raise RuntimeError("cannot_confirm_target: direct UI sending disabled")


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
