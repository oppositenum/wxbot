"""消息分类器：把一条消息(顶层 type + 原始 body)归到稳定的 category(供规则按类型匹配)。

category 用**稳定英文 slug**(规则/代码用),category_name 是中文(展示用)。
顶层 type 覆盖文字/图片/语音/视频/表情/位置/名片/通话/系统;type=49(appmsg)再按
`<appmsg><type>` 子类型 + `<wcpayinfo>`/关键词细分出 红包/转账/文件/链接/小程序/
视频号/聊天记录/引用。红包/转账用"关键词+子类型"双重判定(不硬依赖某个子类型码,
跨微信版本更稳),并抽取金额/备注/文件名等 meta 供规则/动作使用。
"""
import re

# slug -> 中文名
CATEGORIES = {
    "text": "文字", "image": "图片", "voice": "语音", "video": "视频",
    "sticker": "表情", "location": "位置", "card": "名片", "file": "文件",
    "link": "链接", "miniapp": "小程序", "video_channel": "视频号",
    "merged": "聊天记录", "red_packet": "红包", "transfer": "转账",
    "voip": "音视频通话", "quote": "引用", "system": "系统", "revoke": "撤回",
    "other": "其他",
}

# 顶层 local_type -> category(不含需要拆 appmsg 的 49)
_TOP = {
    1: "text", 3: "image", 34: "voice", 42: "card", 43: "video",
    47: "sticker", 48: "location", 50: "voip", 10000: "system", 10002: "revoke",
}

# type=49 的 <appmsg><type> -> category
_APPMSG = {
    "5": "link", "6": "file", "74": "file", "8": "sticker", "17": "location",
    "19": "merged", "33": "miniapp", "36": "miniapp", "51": "video_channel",
    "63": "video_channel", "57": "quote", "2000": "transfer", "2001": "red_packet",
    "2003": "red_packet",
}

_RE_APPTYPE = re.compile(r"<appmsg\b[^>]*>.*?<type>\s*(\d+)\s*</type>", re.S)
_RE_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_RE_DES = re.compile(r"<des>(.*?)</des>", re.S)
_RE_URL = re.compile(r"<url>(.*?)</url>", re.S)
_RE_FEEDESC = re.compile(r"<feedesc>(.*?)</feedesc>", re.S)
_RE_PAYSUB = re.compile(r"<paysubtype>\s*(\d+)\s*</paysubtype>", re.S)
_RE_SCENE = re.compile(r"<scenetext>(.*?)</scenetext>", re.S)
_RE_TOTALLEN = re.compile(r"<totallen>\s*(\d+)\s*</totallen>", re.S)
_RE_FILEEXT = re.compile(r"<fileext>(.*?)</fileext>", re.S)
_RE_MONEY = re.compile(r"[¥￥]\s*([\d.]+)")


def _cdata(s):
    if s is None:
        return ""
    return s.replace("<![CDATA[", "").replace("]]>", "").strip()


def _pick(rx, body):
    m = rx.search(body or "")
    return _cdata(m.group(1)) if m else ""


def _is_payment(body):
    """是否红包/转账消息(有 wcpayinfo 或 wxpay 原生跳转 或 支付关键词)。"""
    b = body or ""
    return ("<wcpayinfo>" in b or "wxpay" in b.lower()
            or "微信红包" in b or "收到转账" in b or "发起了转账" in b)


def _pay_category(body):
    """区分红包 vs 转账(关键词优先,子类型兜底)。"""
    b = body or ""
    scene = _pick(_RE_SCENE, b)
    title = _pick(_RE_TITLE, b)
    des = _pick(_RE_DES, b)
    blob = f"{scene} {title} {des}"
    if "红包" in blob or "恭喜发财" in blob:
        return "red_packet"
    if "转账" in blob:
        return "transfer"
    at = _pick(_RE_APPTYPE, b)
    if at in ("2001", "2003"):
        return "red_packet"
    if at == "2000":
        return "transfer"
    return "transfer"          # 有 wcpayinfo 但认不出细分,默认按转账


def _pay_meta(body):
    m = {}
    fee = _pick(_RE_FEEDESC, body)
    amt = _RE_MONEY.search(fee) or _RE_MONEY.search(body or "")
    if amt:
        m["amount"] = amt.group(1)
    if fee:
        m["fee_desc"] = fee
    sub = _pick(_RE_PAYSUB, body)
    if sub:
        m["paysubtype"] = sub
    des = _pick(_RE_DES, body)
    if des:
        m["memo"] = des
    return m


def classify(real_type, body):
    """返回 (category_slug, category_name, meta_dict)。
    real_type: 顶层 local_type & 0xFFFF; body: 解压且去群发送者前缀后的原始内容(可能是XML)。"""
    b = body or ""
    # 撤回(可能以 10000 系统消息或 10002 出现)
    if real_type == 10002 or "revokemsg" in b:
        return "revoke", CATEGORIES["revoke"], {}

    if real_type in _TOP and real_type != 49:
        cat = _TOP[real_type]
        # 系统消息里混着的撤回已在上面处理;其余系统按 system
        return cat, CATEGORIES[cat], {}

    if real_type == 49:
        # 支付类优先(红包/转账),再看 appmsg 子类型
        if _is_payment(b):
            cat = _pay_category(b)
            return cat, CATEGORIES[cat], _pay_meta(b)
        at = _pick(_RE_APPTYPE, b)
        cat = _APPMSG.get(at)
        if cat == "file":
            meta = {}
            title = _pick(_RE_TITLE, b)
            if title:
                meta["filename"] = title
            ext = _pick(_RE_FILEEXT, b)
            if ext:
                meta["ext"] = ext
            size = _pick(_RE_TOTALLEN, b)
            if size:
                meta["size"] = int(size)
            return cat, CATEGORIES[cat], meta
        if cat == "link":
            meta = {}
            for k, rx in (("title", _RE_TITLE), ("des", _RE_DES), ("url", _RE_URL)):
                v = _pick(rx, b)
                if v:
                    meta[k] = v
            return cat, CATEGORIES[cat], meta
        if cat:
            meta = {}
            title = _pick(_RE_TITLE, b)
            if title:
                meta["title"] = title
            return cat, CATEGORIES[cat], meta
        # 认不出的 appmsg → 链接/其他
        return "link", CATEGORIES["link"], {}

    if real_type == 10000:
        return "system", CATEGORIES["system"], {}
    return "other", CATEGORIES["other"], {}
