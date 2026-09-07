"""强制解密某会话的历史图片：驱动容器里的微信打开大图查看器，用方向键翻遍，
微信会把每张图的明文写到 temp/ImageUtils/，之后网页即可显示。

原理：收到的图片 .dat 用的是取不到的混淆 AES 密钥，无法自行解密；但微信显示大图时
会自己解密落盘。所以点开一张图→进查看器→左右方向键翻遍全部图，全部落明文。
"""
import glob
import os
import re
import subprocess as sp
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from core import docker_wx  # noqa: E402

CONTAINER = docker_wx.CONTAINER
DISPLAY = ":0"
LOCAL = docker_wx.LOCAL


def _sh(*args, timeout=30):
    return docker_wx._exec(*args, timeout=timeout)


def _x(*args, timeout=30):
    return _sh("bash", "-lc", f"DISPLAY={DISPLAY} " + " ".join(args), timeout=timeout)


def _temp_dir():
    return os.path.join(os.path.dirname(config.db_storage_dir()),
                        "temp", "ImageUtils")


def _temp_count():
    try:
        return len(glob.glob(os.path.join(_temp_dir(), "*.jpg")))
    except Exception:  # noqa: BLE001
        return 0


def _screenshot():
    """截图并载入为 PIL Image。"""
    from PIL import Image
    if LOCAL:
        _x("scrot", "-o", "/tmp/_hv_host.png")
    else:
        _x("scrot", "-o", "/tmp/_hv.png")
        sp.run(["docker", "cp", f"{CONTAINER}:/tmp/_hv.png", "/tmp/_hv_host.png"],
               capture_output=True)
    return Image.open("/tmp/_hv_host.png").convert("RGB")


def _find_photos(im):
    """在消息面板里找真实照片(大块高彩色方形)，返回候选中心列表(大→小)。
    表情通常 <100px、纯文字气泡低彩色，会被排除。"""
    W, H = im.size
    px = im.load()
    X0, X1, Y0, Y1 = 520, W - 40, 135, H - 250
    CELL = 24

    def cell_ok(cx, cy):
        cols = set()
        s = s2 = n = 0
        for x in range(cx, min(cx + CELL, X1), 4):
            for y in range(cy, min(cy + CELL, Y1), 4):
                r, g, b = px[x, y]
                cols.add((r >> 5, g >> 5, b >> 5))
                lum = (r + g + b) // 3
                s += lum
                s2 += lum * lum
                n += 1
        if not n:
            return False
        std = (s2 / n - (s / n) ** 2) ** 0.5
        return len(cols) >= 8 and std > 20

    grid = {}
    for cy in range(Y0, Y1, CELL):
        for cx in range(X0, X1, CELL):
            grid[(cx, cy)] = cell_ok(cx, cy)

    import collections
    seen = set()
    out = []
    for key, v in grid.items():
        if not v or key in seen:
            continue
        q = collections.deque([key])
        comp = []
        while q:
            k = q.popleft()
            if k in seen or not grid.get(k):
                continue
            seen.add(k)
            comp.append(k)
            cx, cy = k
            for dx, dy in ((CELL, 0), (-CELL, 0), (0, CELL), (0, -CELL)):
                q.append((cx + dx, cy + dy))
        xs = [c[0] for c in comp]
        ys = [c[1] for c in comp]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if w >= 90 and h >= 90:               # 够大才是照片(排除头像/表情/小图)
            cx = (min(xs) + max(xs)) // 2 + CELL // 2
            cy = (min(ys) + max(ys)) // 2 + CELL // 2
            out.append((len(comp), cx, cy))
    out.sort(reverse=True)
    return [(x, y) for _, x, y in out]


def _find_photo(im):
    pts = _find_photos(im)
    return pts[0] if pts else None


def _click(x, y):
    _x("xdotool", "mousemove", str(x), str(y), "click", "1")


def _key(k, times=1, delay=0.4):
    for _ in range(times):
        if docker_wx.priority_pending():       # 有发送在等→立刻停手让位
            return
        _x("xdotool", "key", k)
        time.sleep(delay)


def _scroll_up(n=3):
    _x("xdotool", "mousemove", "800", "380")
    for _ in range(n):
        _x("xdotool", "click", "4")
        time.sleep(0.2)


def harvest(display_name=None, nav=60, log=print):
    """打开会话(可选)并翻遍其图片，触发微信解密落盘。返回新解密的图片数。
    全程持有 UI 锁，避免与机器人发消息抢微信窗口。"""
    with docker_wx.UI_LOCK:
        return _harvest_locked(display_name, nav, log)


def _vision_shot_bytes():
    _screenshot()
    return open("/tmp/_hv_host.png", "rb").read()


def _vision_title():
    """读右上角当前聊天对象名称，用于校验会话是否真的打开了。"""
    from core import llm
    try:
        t = llm.describe_image(
            _vision_shot_bytes(), media_type="image/png",
            prompt="这是微信截图。只输出右上角标题栏里【当前聊天对象的名称】，"
                   "不带群人数括号；若右侧没打开任何聊天(空白)就输出 EMPTY。只输出名称。")
        return (t or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _vision_open(name, log):
    """视觉定位左侧会话行→点击→校验标题真的打开了，未开就重试(消除滚动/点击竞态)。"""
    from core import llm
    LIST_X = 340
    locate = ("这是微信桌面截图，左侧竖排是会话列表。找到名称正好是"
              f"\"{name}\"的那一行会话，只输出该行中心像素 y 坐标(纯整数)；"
              "找不到输出 -1。只输出一个数字。")

    def scroll_top():
        _x("xdotool", "mousemove", "340", "300")
        for _ in range(25):
            _x("xdotool", "click", "4")
            time.sleep(0.05)
        time.sleep(1.4)

    for attempt in range(5):
        if docker_wx.priority_pending():       # 发送优先：别再翻了
            return False
        scroll_top()
        found_y = -1
        for sweep in range(5):                 # 顶→下逐屏找该行
            if docker_wx.priority_pending():
                return False
            try:
                m = re.search(r"-?\d+", llm.describe_image(
                    _vision_shot_bytes(), media_type="image/png", prompt=locate) or "")
                y = int(m.group()) if m else -1
            except Exception:  # noqa: BLE001
                y = -1
            if y > 120:
                found_y = y
                break
            _x("xdotool", "mousemove", "340", "430")   # 下滚一屏再找
            for _ in range(4):
                _x("xdotool", "click", "5")
                time.sleep(0.12)
            time.sleep(0.6)
        if found_y < 0:
            continue
        _click(LIST_X, found_y)
        time.sleep(1.6)                        # 等聊天加载
        title = _vision_title()
        if title and title != "EMPTY" and (name in title or title in name):
            log(f"打开会话「{name}」@y={found_y} (校验:{title})")
            return True
        log(f"点击后未打开(标题={title!r})，重试")
    log(f"视觉未能可靠打开会话「{name}」")
    return False


def _vision_find_photos(log):
    """视觉在当前聊天窗口里找真实照片(排除表情/贴纸/头像/纯文字),返回坐标列表(可能多张)。"""
    from core import llm
    try:
        _screenshot()
        b = open("/tmp/_hv_host.png", "rb").read()
        txt = llm.describe_image(
            b, media_type="image/png",
            prompt=("这是微信聊天窗口截图，右侧大区域是消息区。请在消息区里找出所有"
                    "【真实照片】(风景/人物/截图等，不要表情包/贴纸/小头像/纯文字气泡)。"
                    "对每张照片输出其中心像素坐标，一行一个，格式严格为 x,y (两个整数逗号分隔)。"
                    "最多输出3张。若没有真实照片，只输出 none。不要输出任何别的文字。"))
        out = []
        for line in (txt or "").splitlines():
            m = re.findall(r"\d+", line)
            if len(m) >= 2:
                x, y = int(m[0]), int(m[1])
                if 460 < x < 1180 and 130 < y < 640:   # 限定在消息区
                    out.append((x, y))
        return out
    except Exception as e:  # noqa: BLE001
        log(f"视觉找图出错: {e}")
        return []


def _harvest_locked(display_name, nav, log):
    before = _temp_count()
    wid = None
    try:
        r = _sh("bash", "-lc",
                f'DISPLAY={DISPLAY} xdotool search --name "^微信$" | tail -1')
        wid = (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        pass
    if wid:
        _x("xdotool", "windowactivate", "--sync", wid)
        time.sleep(0.4)
    if display_name:
        _vision_open(display_name, log)        # 视觉定位点开(可靠)，取代不稳的搜索

    # 打开后微信常停在中间历史位置；先滚到底部(最新消息,新图在这)，再从底往上翻找
    _x("xdotool", "mousemove", "800", "400")
    for _ in range(18):
        _x("xdotool", "click", "5")            # 滚轮下滚到底
        time.sleep(0.08)
    time.sleep(1.2)

    opened = False
    for attempt in range(10):                 # 视觉找一张真实照片→点开→进查看器
        if docker_wx.priority_pending():       # 有发送在等→提前收手，把 UI 让给发送
            log("检测到发送请求，harvest 让位")
            _x("xdotool", "key", "Escape")
            return _temp_count() - before
        pts = _vision_find_photos(log)         # 视觉判定"真实照片"坐标(排除表情/头像/文字)
        if not pts:
            try:                               # 视觉没给→退回像素检测器兜底
                pts = _find_photos(_screenshot())
            except Exception:  # noqa: BLE001
                pts = []
        for pt in pts[:4]:
            c0 = _temp_count()
            _click(*pt)
            time.sleep(1.5)
            if _temp_count() > c0:            # 落明文了 = 查看器打开了
                opened = True
                log(f"已打开大图查看器 @ {pt}")
                break
            _x("xdotool", "key", "Escape")    # 不是照片(表情/视频)，退出再试下一个
            time.sleep(0.3)
        if opened:
            break
        _scroll_up(3)                          # 本屏没有可开的图，上滚找更多

    if not opened:
        log("没找到可点开的照片(可能该会话没有普通图片，或都是表情/视频)")
        return 0

    _key("Left", nav, 0.35)                    # 往历史方向翻遍
    _key("Right", nav // 2, 0.35)              # 回翻覆盖点击点之后的
    _x("xdotool", "key", "Escape")
    after = _temp_count()
    got = after - before
    log(f"本次新解密 {got} 张图片(temp: {before}→{after})")
    return got


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else None
    print("harvested:", harvest(name))
