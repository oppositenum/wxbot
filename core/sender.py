"""UI 自动化发送：文本 / 图片。

原理：设置剪贴板 → 激活微信 → 搜索目标会话 → 打开 → 粘贴 → 回车。
依赖：PyObjC(AppKit/Quartz)。需授权「辅助功能」给运行本进程的程序（终端/Python）。

命令行：
    python3 -m core.sender text  "文件传输助手" "你好"
    python3 -m core.sender image "文件传输助手" /path/to/pic.png
"""
import sys
import time

import AppKit
import Quartz
import ApplicationServices as AX

WECHAT_BUNDLE = "com.tencent.xinWeChat"

# 虚拟键码
KEY_V = 9
KEY_F = 3
KEY_A = 0
KEY_RETURN = 36
CMD = Quartz.kCGEventFlagMaskCommand

# 步骤间延时（秒）——微信响应慢时可调大
DELAYS = {
    "activate": 0.8,
    "after_click_search": 0.4,
    "after_paste_query": 0.9,   # 等侧栏搜索结果出现
    "after_open": 1.0,
    "after_paste_body": 0.4,
}
# 侧栏搜索框相对主窗口左上角的偏移（点）
SEARCH_DX = 180
SEARCH_DY = 47


# ---------- 剪贴板 ----------
def set_clipboard_text(text):
    pb = AppKit.NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, AppKit.NSPasteboardTypeString)


def set_clipboard_image(path):
    img = AppKit.NSImage.alloc().initWithContentsOfFile_(path)
    if img is None:
        raise ValueError(f"无法读取图片：{path}")
    pb = AppKit.NSPasteboard.generalPasteboard()
    pb.clearContents()
    ok = pb.writeObjects_([img])
    if not ok:
        raise RuntimeError("写入图片到剪贴板失败")


# ---------- 键盘事件 ----------
def _post_key(keycode, flags=0):
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    down = Quartz.CGEventCreateKeyboardEvent(src, keycode, True)
    up = Quartz.CGEventCreateKeyboardEvent(src, keycode, False)
    if flags:
        Quartz.CGEventSetFlags(down, flags)
        Quartz.CGEventSetFlags(up, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    time.sleep(0.03)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
    time.sleep(0.03)


def paste():
    _post_key(KEY_V, CMD)


def press_return():
    _post_key(KEY_RETURN)


def select_all():
    _post_key(KEY_A, CMD)


def mouse_click(x, y):
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    pt = Quartz.CGPointMake(x, y)
    move = Quartz.CGEventCreateMouseEvent(src, Quartz.kCGEventMouseMoved, pt, 0)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    time.sleep(0.05)
    down = Quartz.CGEventCreateMouseEvent(src, Quartz.kCGEventLeftMouseDown, pt,
                                          Quartz.kCGMouseButtonLeft)
    up = Quartz.CGEventCreateMouseEvent(src, Quartz.kCGEventLeftMouseUp, pt,
                                        Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    time.sleep(0.05)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def _ax_val(el, attr):
    err, v = AX.AXUIElementCopyAttributeValue(el, attr, None)
    return v if err == 0 else None


def main_window_frame():
    """返回微信主窗口 (x, y, w, h)；失败返回 None。"""
    ws = AppKit.NSWorkspace.sharedWorkspace()
    apps = [a for a in ws.runningApplications()
            if a.bundleIdentifier() == WECHAT_BUNDLE]
    if not apps:
        return None
    axapp = AX.AXUIElementCreateApplication(apps[0].processIdentifier())
    err, wins = AX.AXUIElementCopyAttributeValue(axapp, "AXWindows", None)
    if err != 0 or not wins:
        return None
    for w in wins:
        if _ax_val(w, "AXTitle") == "微信":
            pos = _ax_val(w, "AXPosition")
            size = _ax_val(w, "AXSize")
            if pos and size:
                p = AX.AXValueGetValue(pos, AX.kAXValueCGPointType, None)[1]
                s = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)[1]
                return (p.x, p.y, s.width, s.height)
    return None


def click_sidebar_search():
    """点击侧栏搜索框（不是 Cmd+F 的搜一搜）。"""
    fr = main_window_frame()
    if not fr:
        raise RuntimeError("找不到微信主窗口")
    x, y, _, _ = fr
    mouse_click(x + SEARCH_DX, y + SEARCH_DY)


# ---------- 应用控制 ----------
def activate_wechat():
    ws = AppKit.NSWorkspace.sharedWorkspace()
    apps = [a for a in ws.runningApplications()
            if a.bundleIdentifier() == WECHAT_BUNDLE]
    if not apps:
        raise RuntimeError("微信未运行")
    app = apps[0]
    app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    return app


def is_running():
    ws = AppKit.NSWorkspace.sharedWorkspace()
    return any(a.bundleIdentifier() == WECHAT_BUNDLE
              for a in ws.runningApplications())


# ---------- 高层动作 ----------
def open_chat(target_name):
    """通过侧栏搜索打开与 target_name 的会话。target_name 用备注/昵称/群名。"""
    activate_wechat()
    time.sleep(DELAYS["activate"])
    click_sidebar_search()
    time.sleep(DELAYS["after_click_search"])
    select_all()          # 清掉搜索框里可能的残留
    time.sleep(0.1)
    set_clipboard_text(target_name)
    paste()
    time.sleep(DELAYS["after_paste_query"])
    press_return()        # 打开首个匹配的会话
    time.sleep(DELAYS["after_open"])


def send_text(target_name, text):
    open_chat(target_name)
    set_clipboard_text(text)
    paste()
    time.sleep(DELAYS["after_paste_body"])
    press_return()
    return {"ok": True, "target": target_name, "type": "text"}


def send_image(target_name, image_path):
    open_chat(target_name)
    set_clipboard_image(image_path)
    paste()
    time.sleep(DELAYS["after_paste_body"])
    press_return()
    return {"ok": True, "target": target_name, "type": "image"}


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    kind, target = sys.argv[1], sys.argv[2]
    payload = sys.argv[3]
    if kind == "text":
        print(send_text(target, payload))
    elif kind == "image":
        print(send_image(target, payload))
    else:
        print("kind 必须是 text 或 image")
        sys.exit(1)


if __name__ == "__main__":
    main()
