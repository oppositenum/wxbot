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
import os
import subprocess as sp
import sys
import time

DISPLAY = ":0"
# Chinese UI title is required for normal operation. After a locale-less
# container recreate WeChat may briefly open as WeChat/Weixin; accept those
# exact main-window titles so sending does not die with ERR:no-window.
MAIN_WINDOW_NAMES = ("微信", "WeChat", "Weixin")


def x(*args, **kw):
    return sp.run(args, env={"DISPLAY": DISPLAY, "PATH": "/usr/bin:/bin"},
                  capture_output=True, text=True, **kw)


def main_window_ids():
    """Return candidate main-window IDs, Chinese title first.

    xdotool name search is case-insensitive, so ^WeChat$ can also hit the
    lowercase Qt helper titled wechat. Always verify the exact title.
    After a WeChat relaunch there may be more than one「微信」frame; callers
    should prefer the active or largest one via win_id().
    """
    ids = []
    for title in MAIN_WINDOW_NAMES:
        r = x("xdotool", "search", "--name", "^" + title + "$")
        for wid in r.stdout.split():
            if not wid.strip():
                continue
            name = x("xdotool", "getwindowname", wid).stdout.strip()
            if name == title:
                ids.append(wid)
    return list(dict.fromkeys(ids))


# 微信 4.1.9+ 窗口顶部多了一行自绘标题栏,侧栏搜索框、会话标题整体下移约 19px,
# 会话区也更靠右。按安装的微信版本选一组偏移(相对窗口客户区左上角)。
_LAYOUTS = {
    "4.1.8": {"search": (174, 40), "search_clear": (268, 48), "panel": (60, 20, 430, 620),
              "header": (286, 6, 700, 52), "field_scan_end": 110},
    "4.1.13": {"search": (169, 59), "search_clear": (250, 59), "panel": (77, 40, 430, 640),
               "header": (320, 36, 700, 84), "field_scan_end": 130},
}
_layout_cache = {}


def wechat_version():
    if "v" not in _layout_cache:
        r = x("dpkg-query", "-W", "-f=${Version}", "wechat")
        _layout_cache["v"] = (r.stdout or "").strip()
    return _layout_cache["v"]


def layout():
    try:
        v = tuple(int(p) for p in wechat_version().split(".")[:3])
    except ValueError:
        v = ()
    return _LAYOUTS["4.1.13" if v >= (4, 1, 9) else "4.1.8"]


def new_layout():
    return layout() is _LAYOUTS["4.1.13"]


def _viewable(wid):
    r = x("xwininfo", "-id", str(wid))
    return "IsViewable" in (r.stdout or "")


def _tray_icon_point():
    """面板托盘里的微信图标:截顶栏右半边,找绿色像素团的中心。"""
    import uuid
    path = f"/tmp/wx_tray_{uuid.uuid4().hex[:8]}.png"
    x("scrot", "-o", path)
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
    except Exception:
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    xs, ys = [], []
    for sx in range(im.width // 2, im.width):
        for sy in range(0, 26):
            r, g, b = im.getpixel((sx, sy))
            if g > 150 and r < 90 and b < 120:
                xs.append(sx)
                ys.append(sy)
    if len(xs) < 30:
        return None
    return (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2


def restore_main(wid=None):
    """4.1.9+ 焦点在主窗口时按 Esc 会把窗口收进托盘(窗口 unmap)。
    xdotool windowmap 只让窗口管理器以为它显示了,Qt 不重绘,是空白窗口;只能点托盘图标。"""
    ids = [str(wid)] if wid else main_window_ids()
    if not ids or any(_viewable(i) for i in ids):
        return True
    pt = _tray_icon_point()
    if not pt:
        return False
    x("xdotool", "mousemove", str(pt[0]), str(pt[1]), "click", "1")
    for _ in range(15):
        time.sleep(0.2)
        if any(_viewable(i) for i in (ids if wid else main_window_ids())):
            time.sleep(0.4)
            return True
    return False


def esc():
    """按 Esc。新版若因此把主窗口收进托盘,立刻点托盘恢复。"""
    key("Escape")
    if new_layout():
        time.sleep(0.2)
        restore_main()


def win_id():
    ids = main_window_ids()
    if not ids:
        return None
    active = x("xdotool", "getactivewindow").stdout.strip()
    if active in ids:
        return active
    # Prefer the largest mapped frame; tiny/helper shells are the wrong target.
    best, best_area = ids[0], -1
    for wid in ids:
        _px, _py, w, h = win_geom(wid)
        area = max(0, w) * max(0, h)
        if area > best_area:
            best, best_area = wid, area
    return best


def _window_title_is(name, expect):
    """独立群窗口标题是「群名 (6)」。人数不参与核对。"""
    import re
    got = re.sub(r"\s*\(\d+\)\s*$", "", (name or "").strip())
    return got == (expect or "").strip()


def detached_chat_id(expect):
    """独立聊天窗。标题等于会话名，群窗口允许末尾带人数。"""
    if not expect:
        return None
    r = x("xdotool", "search", "--name", expect)
    ids = [l for l in r.stdout.split() if l.strip()]
    main = set(main_window_ids())
    for i in reversed(ids):
        name = x("xdotool", "getwindowname", i).stdout.strip()
        if i not in main and _window_title_is(name, expect):
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
    """按 UTF-8 文本写入。剪贴板若正被一张图占着，不声明类型时粘贴出来的不是这段字。"""
    last = None
    for _ in range(3):
        p = sp.Popen(
            ["xclip", "-selection", "clipboard", "-t", "UTF8_STRING"],
            stdin=sp.PIPE, stdout=sp.DEVNULL, stderr=sp.DEVNULL,
            env={"DISPLAY": DISPLAY, "PATH": "/usr/bin:/bin"})
        p.communicate(s.encode("utf-8"))
        time.sleep(0.05)
        last = clipget()
        if last == s:
            return
    return last


def set_clip_image(path):
    mt = "image/png"
    if path.lower().endswith((".jpg", ".jpeg")):
        mt = "image/jpeg"
    sp.run(["xclip", "-selection", "clipboard", "-t", mt, "-i", path], stdout=sp.DEVNULL, stderr=sp.DEVNULL,
           env={"DISPLAY": DISPLAY, "PATH": "/usr/bin:/bin"})


def key(*keys):
    x("xdotool", "key", "--clearmodifiers", *keys)


def clipget():
    # 剪贴板里可能还是刚发的图片(PNG 字节),按 UTF-8 严格解码会直接抛异常
    r = sp.run(["xclip", "-selection", "clipboard", "-o", "-target", "UTF8_STRING"],
               env={"DISPLAY": DISPLAY, "PATH": "/usr/bin:/bin"}, capture_output=True)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


def close_overlays():
    """朋友圈/图片预览/聊天内搜索会盖住主窗口，按键会打到浮层或当前会话。"""
    for title in ("^朋友圈$", "^图片$", "^视频$", "^搜索聊天记录$"):
        r = x("xdotool", "search", "--name", title)
        for oid in [l for l in r.stdout.split() if l.strip()]:
            x("xdotool", "windowactivate", "--sync", oid)
            key("Escape")
            time.sleep(0.15)
            x("xdotool", "windowclose", oid)


def leave_search(wid=None):
    """Abort leftover sidebar search so the next send does not start mid-query."""
    wid = str(wid or win_id() or "")
    if not wid:
        return
    try:
        restore_main(wid)
        keep_window_size(wid)
        x("xdotool", "windowactivate", "--sync", wid)
        x("xdotool", "windowfocus", "--sync", wid)
        if new_layout():
            # 新版:焦点在主窗口时 Esc 会收起窗口;先点进搜索框,一次 Esc 只清空搜索
            px, py, _w, _h = win_geom(wid)
            click_search(px, py)
            time.sleep(0.15)
            esc()
        else:
            for _ in range(3):
                esc()
                time.sleep(0.12)
    except Exception:
        pass


def ensure_focused(wid):
    """windowactivate 在 XFCE 里经常只点亮窗口、键盘焦点还在别处。
    不点标题栏，也不双击：双击会被微信当成标题栏双击，窗口跟着放大。"""
    wid = str(wid)
    leave_search(wid)
    keep_window_size(wid)
    x("xdotool", "windowactivate", "--sync", wid)
    x("xdotool", "windowraise", wid)
    x("xdotool", "windowfocus", "--sync", wid)
    time.sleep(0.2)
    active = x("xdotool", "getactivewindow").stdout.strip()
    # Compare against the window we just focused. A second「微信」frame can
    # make a fresh win_id() disagree even when focus succeeded.
    if active != wid:
        # One retry: click inside the client area (not the title bar).
        px, py, w, h = win_geom(wid)
        if w > 0 and h > 0:
            x("xdotool", "mousemove", str(px + max(80, w // 3)), str(py + max(80, h // 3)), "click", "1")
            x("xdotool", "windowactivate", "--sync", wid)
            x("xdotool", "windowfocus", "--sync", wid)
            time.sleep(0.2)
            active = x("xdotool", "getactivewindow").stdout.strip()
    if active != wid:
        leave_search(wid)
        print("ERR:no-focus"); sys.exit(3)
    return win_geom(wid)


def keep_window_size(wid):
    """微信被拉满后搜索框收不到键盘。用窗口管理器收回普通尺寸，不点最大化按钮。"""
    px, py, w, h = win_geom(wid)
    if w < 1200 and h < 820:
        return
    x("wmctrl", "-i", "-r", wid, "-b", "remove,maximized_vert,maximized_horz")
    time.sleep(0.15)
    x("wmctrl", "-i", "-r", wid, "-e", "0,135,65,1022,741")
    time.sleep(0.25)


def _grab(wid):
    import uuid
    path = f"/tmp/wx_grab_{uuid.uuid4().hex[:8]}.png"
    x("scrot", "-o", path)
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
    except Exception:
        return None
    px, py, w, h = win_geom(wid)
    return im, px, py, w, h


def search_field_point(wid):
    """截图里侧栏那个浅灰圆角框，返回打进微信窗口用的相对坐标。
    窗口外框和客户区有偏差，所以在截图上现找，不再写死。"""
    got = _grab(wid)
    if not got:
        return None
    im, px, py, w, h = got

    def is_field(p):
        r, g, b = p
        return 240 <= r <= 252 and abs(r - g) < 6 and abs(g - b) < 6

    bands = []
    y_end = min(im.height, py + layout()["field_scan_end"])
    # 侧栏搜索框在左图标栏右边。标题栏也是浅色，所以从窗口内容顶再往下才开始找。
    x_start = max(0, px + 50)
    x_end = min(im.width, px + 340)
    for sy in range(py + 8, y_end):
        run = None
        best = None
        for sx in range(x_start, x_end):
            if is_field(im.getpixel((sx, sy))):
                if run is None:
                    run = [sx, sx]
                else:
                    run[1] = sx
            elif run:
                if 70 < run[1] - run[0] < 280 and (best is None or run[1] - run[0] > best[1] - best[0]):
                    best = (run[0], run[1])
                run = None
        if best:
            bands.append((sy, best[0], best[1]))
    if not bands:
        return None
    top_y = bands[0][0]
    top = [b for b in bands if b[0] <= top_y + 28]
    block = [top[0]]
    for b in top[1:]:
        if b[0] - block[-1][0] > 3:
            break
        block.append(b)
    if len(block) < 8:
        return None
    x0 = sorted(b[1] for b in block)[len(block) // 2]
    x1 = sorted(b[2] for b in block)[len(block) // 2]
    # 浅色从标题栏连到搜索框底边。底边往上 28px 是输入区；再高就点到标题栏。
    return (x0 + x1) // 2, block[-1][0] - 28


def click_search(px, py):
    """按住 Ctrl 再单击。普通点击只打在外层窗口上，键盘仍留在输入框；
    双击则会被微信当成标题栏双击，窗口跟着放大。
    搜索框位置见 _LAYOUTS(4.1.8 在窗口内容区 (174, 40))。截图找框会把标题栏算进去，点到框外面。"""
    sx, sy = layout()["search"]
    point = (px + sx, py + sy)
    x("xdotool", "mousemove", str(point[0]), str(point[1]))
    time.sleep(0.08)
    x("xdotool", "keydown", "ctrl")
    time.sleep(0.04)
    x("xdotool", "click", "1")
    time.sleep(0.04)
    x("xdotool", "keyup", "ctrl")
    time.sleep(0.25)
    return point


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
    esc()


def search_panel_image(px, py):
    """侧栏搜索浮层。群结果可能排在联系人、聊天记录下面，裁得比探测用的区域高。"""
    import uuid
    from PIL import Image
    tag = uuid.uuid4().hex[:10]
    full = f"/tmp/wx_search_{tag}_full.png"
    crop = f"/tmp/wx_search_{tag}_crop.png"
    x("scrot", "-o", full)
    im = Image.open(full)
    l, t, r, b = layout()["panel"]
    box = (px + l, py + t, px + r, py + b)
    im.crop(box).save(crop)
    return im, box, crop


def search_panel_text(px, py):
    """侧栏搜索结果区 OCR。用来确认查询词进了搜索，而不是对话框。"""
    try:
        _im, _box, crop = search_panel_image(px, py)
    except Exception:
        return ""
    r = x("tesseract", crop, "stdout", "-l", "chi_sim+eng", "--psm", "6")
    return "".join((r.stdout or "").split())


def _ocr_rows(crop_path):
    """把搜索浮层 OCR 按行拼回。同一行的字会分开输出。"""
    r = x("tesseract", crop_path, "stdout", "-l", "chi_sim", "--psm", "4", "tsv")
    rows = {}
    for line in (r.stdout or "").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 12 or not parts[11].strip():
            continue
        try:
            top = int(parts[7])
            conf = float(parts[10])
            left = int(parts[6])
        except ValueError:
            continue
        if conf < 0:
            continue
        rows.setdefault(top // 8 * 8, []).append((left, parts[11].strip()))
    out = []
    for top, words in rows.items():
        text = "".join(w for _x, w in sorted(words))
        out.append((top, text.replace(" ", "")))
    return out


def _section_label(text):
    """分区标题。文章行里的群名碎片不能当成「群聊」。"""
    t = "".join((text or "").split())
    # 小字「群聊」常被 OCR 拆成上下两截,拼回来是「聊群」。
    if ("群聊" in t or "聊群" in t) and "聊天记录" not in t and len(t) <= 6:
        return "group"
    if ("聊天记录" in t and len(t) <= 8) or (t.endswith("记录") and len(t) <= 5):
        return "history"
    if "联系人" in t and len(t) <= 6:
        return "contact"
    # OCR 会把「搜索网络结果」拆成「网涅索络结果」，只保留「络结果」。
    if ("结果" in t and "聊天" not in t and len(t) <= 12) or "网络" in t:
        return "web"
    return ""


def _name_hits(text, name):
    """这一行是不是目标群名。相近文章标题不算。"""
    q = "".join((name or "").split())
    t = "".join((text or "").split())
    if not q or not t or len(t) > len(q) + 4:
        return False
    return q in t


def _panel_floor(im):
    """搜索浮层铺着一片浅灰。这片灰结束、出现会话分隔线时就是卡片底。

    分隔线下面是公众号和文件传输助手，不能当成群。
    """
    def gray_share(y):
        hit = seen = 0
        for x in range(80, min(im.width, 330), 2):
            r, g, b = im.getpixel((x, y))[:3]
            seen += 1
            if 235 <= min(r, g, b) <= 246 and max(r, g, b) - min(r, g, b) < 8:
                hit += 1
        return hit / seen if seen else 0

    if new_layout():
        # 新版浮层是白底,只有高亮的那条建议是灰的,按灰色找底边会停在第三行。
        # 交给分区标题(聊天记录/联系人)截止。
        return im.height
    seen_card = False
    for y in range(30, im.height - 2):
        if gray_share(y) >= 0.8:
            seen_card = True
            continue
        if seen_card and gray_share(y) < 0.3 and gray_share(y + 1) < 0.3:
            return y
    return im.height


def _result_rows(im):
    """搜索结果里带头像的行，返回 (y, 是不是群的四宫格头像)。

    联系人是一张完整头像。群是四张小图拼成的方块，中间有浅色十字缝。
    网络结果只有灰色放大镜，没有这块彩色头像。
    """
    x1 = min(48, im.width)
    floor = _panel_floor(im)
    hits = []
    for y in range(im.height):
        colorful = 0
        for x in range(8, x1):
            r, g, b = im.getpixel((x, y))[:3]
            if max(r, g, b) - min(r, g, b) >= 18 and min(r, g, b) < 235:
                colorful += 1
        if y >= floor:
            break
        if colorful >= 6:
            hits.append(y)
    bands = []
    start = prev = None
    for y in hits + [10 ** 9]:
        if start is None:
            start = prev = y
            continue
        if y > prev + 3:
            if 16 <= prev - start + 1 <= 64:
                bands.append((start, prev))
            start = y
        prev = y
    rows = []
    for top, bottom in bands:
        seam = 0
        seen = 0
        # 十字缝可能偏上或偏下，扫中间一带，不赌正好在中线。
        for y in range(top + 4, bottom - 3):
            for x in range(8, x1):
                r, g, b = im.getpixel((x, y))[:3]
                if min(r, g, b) >= 200 and max(r, g, b) - min(r, g, b) < 24:
                    seam += 1
                seen += 1
        rows.append(((top + bottom) // 2, bool(seen) and seam / seen >= 0.06))
    return rows


def _joined_rows(rows):
    """上下挨着的 OCR 碎片拼回一行。群名的最后一个字经常被拆到上一截。"""
    out = []
    for top, text in sorted(rows):
        if out and top - out[-1][0] <= 16:
            out[-1][1] += text
            continue
        out.append([top, text])
    return [(top, text) for top, text in out]


def _next_section(rows, floor):
    """群行到此为止。再往下是网络文章或聊天记录，不能点。"""
    tops = [top for top, text in rows
            if top > floor and _section_label(text) in ("web", "history", "contact")]
    return min(tops) if tops else None


def _group_row_from_rows(rows, panel, name):
    """返回群那一行在裁切图里的 y。找不到返回空。

    群在「群聊」标题和「搜索网络结果」之间，左边是头像。OCR 会把群名拆碎，
    也会在前面认错一两个字，所以这个区间里只有一个头像时直接点它。
    没有「群聊」标题时，只点名字对得上的那一行。
    """
    rows = _joined_rows(rows)
    label_top = None
    for top, text in rows:
        if _section_label(text) == "group" and (label_top is None or top < label_top):
            label_top = top
    floor = 0 if label_top is None else label_top + 12
    ceiling = _next_section(rows, floor)
    if ceiling is None:
        ceiling = panel.height
    named = [top for top, text in rows if floor <= top < ceiling and _name_hits(text, name)]
    found = [(y, is_group) for y, is_group in _result_rows(panel) if floor <= y < ceiling]
    avatars = [y for y, _is_group in found]
    if label_top is not None and len(avatars) == 1:
        return avatars[0]
    # 「群聊」被裁掉、群名少认最后一个字时，卡片里唯一的群头像就是这一行。
    groups = [y for y, is_group in found if is_group]
    q = "".join((name or "").split())
    # OCR 常把群名最后一个字裁掉，并和上一行的碎片粘在一起。
    stem = q[:-1] if len(q) >= 5 else q
    prefix = any(stem and stem in "".join((text or "").split())
                 for _top, text in rows if floor <= _top < ceiling)
    if label_top is None and len(groups) == 1 and prefix:
        return groups[0]
    near = [y for y in avatars if any(abs(y - top) <= 28 for top in named)]
    if len(near) == 1:
        return near[0]
    if label_top is not None and len(named) == 1:
        return named[0] + 10
    return None


def _group_row_y(im, box, crop_path, name=""):
    """返回「群聊」分区里、名字对得上的那一行的屏幕 y。

    搜群名时，浮层最上面可能是网络文章，回车打开的是文章。
    真正的群在「群聊」标题下面，头像是多人图。再往下的「聊天记录」是私聊，
    不能点。认不出群这一行就返回空，调用方不回车。
    """
    full_screen = im.size[0] > (box[2] - box[0] + 4)
    try:
        panel = im.crop(box) if full_screen else im
    except Exception:
        panel = im
        full_screen = False
    y = _group_row_from_rows(_ocr_rows(crop_path), panel, name)
    # 整屏截图里，行 y 要加上裁图的屏幕起点。传入的就是裁图时，y 已经在裁图内。
    if y is None:
        return None
    return box[1] + y if full_screen else y


def search_shows_query(px, py, name, held=""):
    q = "".join((name or "").split())
    if not q:
        return False
    held_l = "".join((held or focused_text() or "").split()).lower()
    q_l = q.lower()
    if q_l and (q_l in held_l or held_l in q_l):
        return True
    got = search_panel_text(px, py)
    got_l = (got or "").lower()
    if q_l in got_l:
        return True
    # Lossy Chinese OCR: enough shared characters, or a live search-result chrome.
    overlap = sum(1 for c in q if c in (got or ""))
    if overlap >= max(2, (len(q) + 1) // 2):
        return True
    if any(mark in (got or "") for mark in ("联系人", "搜索网络", "网络查找", "微信号")):
        return True
    return header_matches(got, name)


def focus_search_box(px, py):
    """点侧栏搜索并清空。先打一枚探测字，确认它进了搜索框而不是聊天输入框。"""
    click_search(px, py)
    time.sleep(0.15)
    key("ctrl+a")
    time.sleep(0.05)
    key("Delete")
    time.sleep(0.08)
    x("xdotool", "type", "--clearmodifiers", "--delay", "20", "q")
    time.sleep(0.45)
    shown = search_panel_text(px, py)
    held = focused_text()
    key("BackSpace")
    time.sleep(0.12)
    shown_l = (shown or "").lower()
    held_l = (held or "").lower()
    # OCR often misses the tiny probe letter. A live search panel that already
    # lists「联系人」after typing is enough proof the sidebar search is focused.
    search_ui = ("联系人" in (shown or "")) or ("contact" in shown_l)
    if ("q" not in shown_l) and ("q" not in held_l) and not search_ui:
        abort_if_composer_polluted()
        print("ERR:search-unfocused"); sys.exit(3)
    return True


def search_box_holds(name):
    return focused_text() == name


def dismiss_search(px, py):
    """关掉侧栏搜索浮层。Esc 关不掉这个浮层，要点搜索框右侧的清除。"""
    cx, cy = layout()["search_clear"]
    x("xdotool", "mousemove", str(px + cx), str(py + cy), "click", "1")
    time.sleep(0.2)


def click_search_hit(px, dy, py, clicks=1):
    # 侧栏搜索「联系人」第一条。单击只出资料预览，双击才打开会话。
    # 同样必须打进微信窗口，屏幕坐标会被桌面吃掉。
    # 行尾的 i 是资料按钮。点名字（约 150, 128），别点到行尾。
    x("xdotool", "mousemove", str(px + 150), str(py + dy),
      "click", "--repeat", str(clicks), "--delay", "80", "1")
    time.sleep(0.35)


def _shot_paths():
    """每次截图单独落盘。固定的 /tmp 文件会被别的线程写坏，OCR 就会读到空图。"""
    import uuid
    tag = uuid.uuid4().hex[:10]
    return f"/tmp/wx_header_{tag}_full.png", f"/tmp/wx_header_{tag}_crop.png"


def _ocr_title(path):
    """顶栏字很小，emoji 会把汉字读丢。放大后再认一次，两份结果都参与核对。"""
    reads = []
    r = x("tesseract", path, "stdout", "-l", "chi_sim+eng", "--psm", "7")
    raw = "".join((r.stdout or "").split())
    if raw:
        reads.append(raw)
    try:
        from PIL import Image, ImageEnhance
        im = Image.open(path)
        big = im.resize((im.width * 3, im.height * 3), Image.Resampling.LANCZOS)
        big = ImageEnhance.Contrast(big).enhance(2.0)
        scaled = path + ".x3.png"
        big.save(scaled)
        r2 = x("tesseract", scaled, "stdout", "-l", "chi_sim+eng", "--psm", "7")
        scaled_text = "".join((r2.stdout or "").split())
        if scaled_text and scaled_text not in reads:
            reads.append(scaled_text)
    except Exception:
        pass
    return reads


def _header_box(px, py, w):
    """主窗口顶栏在侧栏右边，紧贴窗口内容顶。
    独立聊天窗没有侧栏，标题在系统标题栏下面一行，从左边开始。"""
    if w >= 900:
        l, t, r, b = layout()["header"]
        return px + l, py + t, px + min(w - 80, r), py + b
    right = max(120, w - 90)
    return px + 8, py + 30, px + right, py + 68


def header_text(px, py, w, h):
    """读会话顶栏标题。OCR 只做核对，不拿来选人。"""
    full, crop = _shot_paths()
    x("scrot", "-o", full)
    try:
        from PIL import Image
        im = Image.open(full)
        im.crop(_header_box(px, py, w)).save(crop)
    except Exception:
        return ""
    return "\n".join(_ocr_title(crop))


# 本进程刚用唯一微信号点开的会话。表情昵称读不准时，只靠这个放行。
_opened_by_alias = ""


def header_matches(got, expect):
    if not expect:
        return True
    # header_text 把原图和放大图的识别结果用换行拼在一起，任一读法对上即可。
    reads = [part for part in (got or "").splitlines() if part.strip()] or [got or ""]
    for raw in reads:
        if _header_one(raw, expect):
            return True
    return False


def _title_parts(text):
    """拆成汉字、拉丁字母数字、表情/符号个数。🌱 和 🐮 不进汉字。"""
    import unicodedata
    han, latin, marks = [], [], 0
    for c in text or "":
        if c.isspace():
            continue
        o = ord(c)
        if "\u4e00" <= c <= "\u9fff":
            han.append(c)
        elif c.isascii() and c.isalnum():
            latin.append(c.lower())
        elif unicodedata.category(c).startswith("S") or o > 0x1F000 or 0xFE00 <= o <= 0xFE0F:
            marks += 1
    return "".join(han), "".join(latin), marks


def _header_one(got, expect):
    a = "".join((got or "").split())
    b = "".join((expect or "").split())
    if not a or not b:
        return False
    if a == b or b in a:
        return True
    # 只读出拉丁尾巴不能当成整段昵称。
    if a in b and len(a) >= max(4, int(len(b) * 0.85)):
        return True
    import difflib
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    # 纯英文短群名必须几乎整词对上，避免和另一个短名糊成同一会话。
    if all(ord(c) < 128 for c in b):
        return b.lower() in a.lower() and ratio >= 0.85
    ha, la, _ma = _title_parts(a)
    hb, lb, mb = _title_parts(b)
    if hb:
        # 差一个同音字算同一顶栏；整词换成另一个名字不是。
        if not ha or difflib.SequenceMatcher(None, ha, hb).ratio() < 0.5:
            return False
        if lb and (lb not in la or len(la) > len(lb) + 2):
            return False
        return ratio >= 0.7 and len(a) >= max(4, int(len(b) * 0.75))
    if mb:
        # 表情加拉丁尾巴去掉表情只剩尾巴。另一套汉字加同一尾巴，不能靠它放行。
        if ha:
            return False
        return ratio >= 0.85 and len(a) >= int(len(b) * 0.9)
    return ratio >= 0.7 and len(a) >= max(4, int(len(b) * 0.75))


def _header_conflicts(got, expect):
    """读出了另一套汉字名。表情读丢可以放过，汉字对不上不能发。"""
    import difflib
    hb, lb, _mb = _title_parts(expect)
    reads = [part for part in (got or "").splitlines() if part.strip()] or [got or ""]
    for raw in reads:
        ha, la, _ma = _title_parts(raw)
        if not ha:
            continue
        if not hb:
            # Expectation was emoji / latin only. OCR often
            # invents a stray Chinese glyph such as「这」; that alone is not the
            # wrong chat. Only conflict when the expected latin tail is gone.
            if lb and lb in (la or ""):
                continue
            if lb and la and lb not in la:
                return True
            continue
        if difflib.SequenceMatcher(None, ha, hb).ratio() < 0.5:
            return True
    return False


def searched_by_wechat_id(name):
    """可搜索的微信号：ASCII、无空格、至少 6 位。纯英文短名是群名，走标题核对。
    没有数字的微信号也要点联系人行，不能靠回车停在当前会话。
    wxid_xxx 是内部号，搜索框搜不到，不能当微信号处理。"""
    s = (name or "").strip()
    if not s or " " in s or "@" in s or any(ord(c) >= 128 for c in s):
        return False
    if s.startswith("wxid_"):
        return False
    return len(s) >= 6


def header_crop_path(px, py, w, h):
    full, crop = _shot_paths()
    x("scrot", "-o", full)
    try:
        from PIL import Image
        im = Image.open(full)
        im.crop(_header_box(px, py, w)).save(crop)
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
    global _opened_by_alias
    if not expect:
        return False
    got = header_text(px, py, w, h)
    if _header_conflicts(got, expect):
        _opened_by_alias = ""
        return False
    if header_matches(got, expect):
        return True
    # 刚用唯一微信号点开，顶栏又读不出另一套汉字，才承认这个会话。
    return bool(_opened_by_alias)


def open_chat(wid, name, expect=""):
    close_overlays()
    px, py, w, h = ensure_focused(wid)
    # 关掉残留搜索浮层；不要在输入框里回车。新版的 leave_search 已经清过搜索,
    # 再按 Esc 只会把主窗口收进托盘。
    if not new_layout():
        esc()
        time.sleep(0.12)
        esc()
        time.sleep(0.12)
    global _opened_by_alias
    px, py, w, h = win_geom(wid)
    if already_on_chat(px, py, w, h, expect):
        return px, py, w, h
    _opened_by_alias = ""
    focus_search_box(px, py)
    # 中文群名逐字打不进搜索框。粘贴后核对剪贴板，避免把别的内容搜出去。
    set_clip_text(name)
    if clipget() != name:
        leave_search()
        print("ERR:search-unfocused"); sys.exit(3)
    # 焦点刚落到搜索框时，xdotool 可能已经打下残留字母。先清掉再粘贴。
    key("ctrl+a")
    time.sleep(0.05)
    key("Delete")
    time.sleep(0.05)
    key("ctrl+v")
    time.sleep(0.3)
    held_after_type = focused_text()
    time.sleep(0.7)          # 等搜索结果
    if not search_shows_query(px, py, name, held=held_after_type):
        abort_if_composer_polluted()
        leave_search()
        print("ERR:search-unfocused"); sys.exit(3)
    before = header_text(px, py, w, h)
    if searched_by_wechat_id(name):
        # 结果浮层还开着时回车打开联系人第一条。点那一行会先关掉浮层，
        # 第二下落到侧栏里的同一行，会话不切换。
        key("Return")
        time.sleep(0.8)
        esc()
        time.sleep(0.15)
        px, py, w, h = win_geom(wid)
        if not pane_has_header(header_crop_path(px, py, w, h)):
            print("ERR:chat-not-opened"); sys.exit(3)
        got = header_text(px, py, w, h)
        if expect and _header_conflicts(got, expect):
            print("ERR:wrong-chat"); sys.exit(3)
        _opened_by_alias = name
        return px, py, w, h
    # 群名：单击「群聊」分区里名字对得上的那一行，再回车打开。
    # 直接回车会打开当前高亮。群名和网络文章标题相近时，高亮是最上面的文章。
    group_y = None
    try:
        panel, box, crop = search_panel_image(px, py)
        group_y = _group_row_y(panel, box, crop, name)
        if new_layout():
            # 新版结果是陆续加载的:先出网络建议,群聊分区随后插到最上面,行位置会变。
            # 连续两次算出同一行才算稳定。
            for _ in range(4):
                time.sleep(0.6)
                panel, box, crop = search_panel_image(px, py)
                again = _group_row_y(panel, box, crop, name)
                if again and group_y and abs(again - group_y) <= 6:
                    break
                group_y = again
            else:
                group_y = None
    except Exception:
        group_y = None
    if not group_y:
        leave_search()
        print("ERR:wrong-chat"); sys.exit(3)
    if new_layout():
        # 新版单击结果行就直接打开会话并收起浮层;旧版的补双击会点到后面的列表。
        click_search_hit(px, group_y - py, py, clicks=1)
        time.sleep(0.8)
        px, py, w, h = win_geom(wid)
        if expect and not header_matches(header_text(px, py, w, h), expect):
            print("ERR:wrong-chat"); sys.exit(3)
        return px, py, w, h
    # 单击能选中群行（名字变绿），但回车打进搜索框，不打开会话。
    # 连续两次双击，第二下会落在浮层后面的列表上。先单击把浮层留住，
    # 停一下再双击，两下都打在这一行上。
    # group_y 是屏幕坐标。click_search_hit 要的是相对窗口顶的行距，
    # 漏传 py 会在点群行时 TypeError，发送器只能记成结果未知。
    row_dy = group_y - py
    click_search_hit(px, row_dy, py, clicks=1)
    time.sleep(0.35)
    click_search_hit(px, row_dy, py, clicks=2)
    time.sleep(0.8)
    # Esc 关不掉这个搜索浮层。点搜索框右侧的清除。
    dismiss_search(px, py)
    px, py, w, h = win_geom(wid)
    if expect and not header_matches(header_text(px, py, w, h), expect):
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
    if expect and not already_on_chat(px, py, w, h, expect):
        print("ERR:wrong-chat"); sys.exit(3)
    focus_input(px, py, w, h)
    if expect and not already_on_chat(px, py, w, h, expect):
        abort_if_composer_polluted()
        print("ERR:wrong-chat"); sys.exit(3)
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
    if expect and not already_on_chat(px, py, w, h, expect):
        print("ERR:wrong-chat"); sys.exit(3)
    focus_input(px, py, w, h)
    if expect and not already_on_chat(px, py, w, h, expect):
        abort_if_composer_polluted()
        print("ERR:wrong-chat"); sys.exit(3)
    clear_input()
    set_clip_image(path)
    time.sleep(0.25)
    key("ctrl+v")
    time.sleep(0.8)
    key("Return")
    time.sleep(0.25)
    click_send(px, py, w, h)
    print("OK")


def _is_green(px_rgb):
    r, g, b = px_rgb[:3]
    return g > 150 and r < 90 and b < 140


def _recording_bar(px, py, w, h):
    """「发语音」按下后,输入栏右侧是绿色圆形发送箭头。用它判断正在录音。"""
    import uuid
    path = f"/tmp/wx_voice_{uuid.uuid4().hex[:8]}.png"
    x("scrot", "-o", path)
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
    except Exception:
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    cx, cy = px + w - 41, py + h - 37
    hits = sum(1 for dx in range(-6, 7, 3) for dy in range(-6, 7, 3)
               if 0 <= cx + dx < im.width and 0 <= cy + dy < im.height and _is_green(im.getpixel((cx + dx, cy + dy))))
    return hits >= 12


def _pulse_env():
    return {"DISPLAY": DISPLAY, "PATH": "/usr/bin:/bin",
            "XDG_RUNTIME_DIR": f"/tmp/runtime-{os.getuid()}"}


def send_voice(name, path, expect=""):
    """发真语音条(微信 4.1.9+):点输入栏右下「发语音」开始录,把音频播进虚拟麦克风 vmic,
    播完点录音条右侧绿色箭头发送。录音条没出现或播放失败就点 ✕ 取消,一定不发。"""
    if not new_layout():
        print("ERR:voice-unsupported"); sys.exit(4)
    px, py, w, h = activate_chat_window(name, expect)
    if expect and not already_on_chat(px, py, w, h, expect):
        print("ERR:wrong-chat"); sys.exit(3)
    focus_input(px, py, w, h)
    if expect and not already_on_chat(px, py, w, h, expect):
        abort_if_composer_polluted()
        print("ERR:wrong-chat"); sys.exit(3)
    clear_input()        # 输入框有字时「发语音」按钮行为不同,先清空
    x("xdotool", "mousemove", str(px + w - 98), str(py + h - 33), "click", "1")
    time.sleep(0.7)
    if not _recording_bar(px, py, w, h):
        print("ERR:voice-ui"); sys.exit(3)
    played = sp.run(["paplay", "-d", "vmic", path], env=_pulse_env(), capture_output=True, text=True)
    time.sleep(0.4)
    if played.returncode != 0 or not _recording_bar(px, py, w, h):
        x("xdotool", "mousemove", str(px + w - 252), str(py + h - 37), "click", "1")   # ✕ 取消录音
        time.sleep(0.3)
        print("ERR:voice-play"); sys.exit(3)
    x("xdotool", "mousemove", str(px + w - 41), str(py + h - 37), "click", "1")
    time.sleep(0.8)
    if _recording_bar(px, py, w, h):
        print("ERR:voice-send-uncertain"); sys.exit(5)
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
    if kind == "leavesearch":     # 清掉残留搜索;新版不会把主窗口收进托盘
        restore_main()
        leave_search()
        restore_main()
        print("OK")
    elif kind == "justopen":
        just_open(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    elif kind == "open":
        open_and_scroll(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 6, expect)
    elif len(sys.argv) < 4:
        print(__doc__); sys.exit(1)
    elif kind == "text":
        send_text(sys.argv[2], sys.argv[3], expect)
    elif kind == "image":
        send_image(sys.argv[2], sys.argv[3], expect)
    elif kind == "voice":         # voice <搜索词> <wav 路径> <会话名>
        send_voice(sys.argv[2], sys.argv[3], expect)
    else:
        print("kind must be text|image|voice|open|justopen|leavesearch"); sys.exit(1)
