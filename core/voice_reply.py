"""机器人用语音条回复:什么时候说语音、用哪个音色。

配置存 work/voice_reply.json:
  enabled   总开关
  mode      off 不说语音 / request 只在对方要求时 / auto 对方要求或模型觉得适合 / always 每条都说
  chance    auto 模式下,模型标了 [[VOICE]] 后实际说语音的概率(0~1),避免条条都是语音
  voice     音色 key(core/tts.py),空=TTS 默认音色
  max_chars 超过这个字数的回复不说语音(长段落听着累,也容易超 60 秒)
  chats     {会话: {enabled/mode/chance/voice/max_chars}} 单个会话覆盖全局

自然语言:
  - 对方这一批里说「发语音/说给我听/想听你声音」等 → 这条用语音;说「别发语音/打字」→ 这条用文字。
  - 「以后都用语音回我」「以后别发语音了」→ 记成这个会话的偏好(mode=always/off)。
  - auto 模式下提示词让模型在适合开口说的回复前加 [[VOICE]](晚安、撒娇、简短口语)。

只有微信 4.1.9+(界面有「发语音」)才可用;战斗、接龙、发图等场景由调用方跳过。
"""
import json
import os
import random
import re
import subprocess
import threading

import config

MARK = "[[VOICE]]"
MODES = ("off", "request", "auto", "always")
DEFAULTS = {"enabled": False, "mode": "auto", "chance": 0.5, "voice": "", "max_chars": 60}

ASK_RE = re.compile(
    r"(发|来|回)(一?[条个段])?语音|语音(回|说|发|聊)|用语音|说给我听|念给我听|读给我听|唱给我听|"
    r"(想|要)听(听)?你(的)?声音|听听你(的)?声音|让我听听|开口说")
DENY_RE = re.compile(r"(别|不要|不用|甭)(再)?(发|回)?语音|打字(吧|说|回)|发文字|文字回|听不了语音|不方便听")
PERSIST_ON_RE = re.compile(r"(以后|之后|今后|从现在起)(都|就|一直)?(用|发|回)语音")
PERSIST_OFF_RE = re.compile(r"(以后|之后|今后|从现在起)(都|就)?(别|不要|不用)(再)?(发|回|用)?语音")

PROMPT = ("\n\n【语音】这条回复可以用语音条发出(会用 TTS 读出来)。"
          "只有当这句话适合开口说——比如晚安、撒娇、安慰、简短的口语回应、对方想听你声音——"
          f"才在回复最开头加 {MARK}(单独标记,不要写进正文)。"
          "长段说明、列表、链接、需要对方看的内容不要加。加了标记时正文写成口语,别用表情符号和颜文字。")

_lock = threading.Lock()
_ver_cache = {}


def _path():
    return os.path.join(config.WORK_DIR, "voice_reply.json")


def _read():
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(d):
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _path())


def _clean(fields):
    out = {}
    if "enabled" in fields:
        out["enabled"] = bool(fields["enabled"])
    if "mode" in fields:
        if fields["mode"] not in MODES:
            raise ValueError("mode 只能是 off / request / auto / always")
        out["mode"] = fields["mode"]
    if "chance" in fields:
        c = float(fields["chance"])
        if not 0 <= c <= 1:
            raise ValueError("chance 要在 0~1 之间")
        out["chance"] = c
    if "voice" in fields:
        v = str(fields["voice"] or "")
        if v:
            from core import tts
            tts.get_voice(v)          # 不存在会抛 TTSError
        out["voice"] = v
    if "max_chars" in fields:
        n = int(fields["max_chars"])
        if not 5 <= n <= 300:
            raise ValueError("max_chars 要在 5~300 之间")
        out["max_chars"] = n
    return out


def get_config():
    d = _read()
    g = dict(DEFAULTS, **{k: v for k, v in d.items() if k in DEFAULTS})
    return dict(g, chats=d.get("chats") or {})


def set_global(**fields):
    with _lock:
        d = _read()
        d.update(_clean(fields))
        _write(d)
    return get_config()


def set_chat(chat, **fields):
    if not chat:
        raise ValueError("缺少会话")
    with _lock:
        d = _read()
        chats = d.setdefault("chats", {})
        chats[chat] = dict(chats.get(chat) or {}, **_clean(fields))
        _write(d)
    return get_config()


def clear_chat(chat):
    with _lock:
        d = _read()
        (d.get("chats") or {}).pop(chat, None)
        _write(d)
    return get_config()


def chat_cfg(chat):
    c = get_config()
    out = {k: c[k] for k in DEFAULTS}
    out.update({k: v for k, v in (c["chats"].get(chat) or {}).items() if k in DEFAULTS})
    return out


def available():
    """微信 4.1.9+ 才有「发语音」。按安装的 deb 版本判断,结果缓存。"""
    if "ok" not in _ver_cache:
        try:
            r = subprocess.run(["dpkg-query", "-W", "-f=${Version}", "wechat"],
                               capture_output=True, text=True, timeout=5)
            v = tuple(int(p) for p in (r.stdout or "").strip().split(".")[:3])
            _ver_cache["ok"] = v >= (4, 1, 9)
        except Exception:
            _ver_cache["ok"] = False
    return _ver_cache["ok"]


def active(chat):
    c = chat_cfg(chat)
    return bool(c["enabled"]) and c["mode"] != "off" and available()


ASKED_LIMIT = 150         # 对方明确要语音时放宽字数(约 40 秒,语音条上限 60 秒)

SPEAK_PROMPT = ("\n\n【语音】这条回复会用语音条发出(你的真实声音,TTS 读出来)。"
                "你能发语音,不要说只能发文字、发不了语音。"
                "正文写成直接说出口的话,{n} 字以内,别用表情符号、颜文字、列表和链接。")


def wants_voice(chat, inbound):
    """这一批消息是否确定要用语音回(对方要求,或会话设成 always)。"""
    if not active(chat) or DENY_RE.search(inbound or ""):
        return False
    return bool(ASK_RE.search(inbound or "")) or chat_cfg(chat)["mode"] == "always"


def prompt(chat, inbound=""):
    """给模型的说明:确定要说语音时让它写短口语;auto 模式时让它自己判断加不加 [[VOICE]]。"""
    c = chat_cfg(chat)
    if wants_voice(chat, inbound):
        n = ASKED_LIMIT if ASK_RE.search(inbound or "") else c["max_chars"]
        return SPEAK_PROMPT.format(n=n)
    return PROMPT if active(chat) and c["mode"] == "auto" else ""


def strip_mark(text):
    return (text or "").replace(MARK, "").strip()


_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
_WX_FACE_RE = re.compile(r"\[[^\[\]\s]{1,6}\]")


def spoken(text):
    """读出来的文本:去掉 [[VOICE]]/[[NEXT]]、微信表情码 [旺柴]、emoji。"""
    t = strip_mark(text).replace("[[NEXT]]", "，")
    t = _WX_FACE_RE.sub("", t)
    t = _EMOJI_RE.sub("", t)
    return re.sub(r"\s+", " ", t).strip(" ，,")


def remember_preference(chat, inbound):
    """对方说「以后都用语音回我/以后别发语音了」时记成这个会话的偏好。返回新 mode 或 None。"""
    if not chat or not inbound or not chat_cfg(chat)["enabled"]:
        return None
    if PERSIST_OFF_RE.search(inbound):
        set_chat(chat, mode="off")
        return "off"
    if PERSIST_ON_RE.search(inbound):
        set_chat(chat, mode="always")
        return "always"
    return None


def decide(chat, inbound, reply, rnd=random.random):
    """这条回复要不要说成语音。返回 (要不要, 原因)。reply 是模型原文(可能带 [[VOICE]])。"""
    if not active(chat):
        return False, "disabled"
    c = chat_cfg(chat)
    inbound = inbound or ""
    if DENY_RE.search(inbound):
        return False, "asked_for_text"
    words = spoken(reply)
    if not words:
        return False, "empty"
    if ASK_RE.search(inbound):
        # 对方点名要语音:放宽字数,只防超出语音条时长
        if len(words) > max(ASKED_LIMIT, c["max_chars"]):
            return False, "too_long"
        return True, "asked_for_voice"
    if len(words) > c["max_chars"]:
        return False, "too_long"
    if c["mode"] == "always":
        return True, "always"
    if c["mode"] == "auto" and MARK in (reply or ""):
        return (rnd() < c["chance"]), "model_marked"
    return False, "not_chosen"


def voice_for(chat):
    return chat_cfg(chat)["voice"] or None
