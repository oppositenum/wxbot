"""「牛来」关键词：看到触发词只处理后面的文字，回复统一加 [牛牛模式]。"""
import re

TRIGGER = "牛来"
PREFIX = "[牛牛模式]"
RULE = {
    "name": "niu",
    "match": {"type": "niu"},
    "action": {"type": "reply_ai", "persona": ""},
}

_LEAD = re.compile(r"^[\s@＠]+")


def payload(text):
    """有触发词则返回后面的正文（可为空串）；没有则 None。"""
    raw = _LEAD.sub("", text or "")
    i = raw.find(TRIGGER)
    if i < 0:
        return None
    rest = raw[i + len(TRIGGER):]
    return rest.lstrip(" \t:：,，.-")


def is_bot_stamp(msg):
    text = (msg or {}).get("content") or ""
    return text.lstrip().startswith(PREFIX)


def is_trigger(msg):
    if (msg or {}).get("type") not in (1, 49, None):
        return False
    text = msg.get("content") or ""
    if is_bot_stamp(msg):
        return False
    rest = payload(text)
    return rest is not None and bool(rest.strip())


def allowed(msg, chat, watch_set):
    """监听中的人或群：谁说「牛来」都处理。未监听会话只处理本账号自己说的。"""
    if not is_trigger(msg):
        return False
    if chat in (watch_set or ()):
        return True
    return bool(msg.get("is_self"))


def inbound(msg):
    """给模型看的入站消息：去掉触发词，当作对方提问。"""
    rest = payload(msg.get("content") or "")
    if rest is None:
        return dict(msg)
    out = dict(msg)
    out["content"] = rest.strip()
    out["is_self"] = False
    out["_niu"] = True
    # reply_context.is_self 还会用 sender==本账号 判断；改成提问方以免整批被滤空。
    if out.get("sender"):
        out["_niu_from"] = out["sender"]
        out["sender"] = "niu:" + str(out["sender"])
    return out


def stamp(text):
    body = (text or "").strip()
    if not body:
        return body
    if body.startswith(PREFIX):
        return body
    return PREFIX + body
