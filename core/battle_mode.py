"""战斗（吵架/辩论）模式：对指定会话逐条回应、据理力争，不看是否 @/引用。

由管理员命令 /开启战斗模式、/关闭战斗模式 控制（见 admin_commands），也可在 UI 里
配置专属人设。状态与人设按账号隔离、落盘持久化，进程重启后仍生效。这里只管「哪些
会话开着」「吵架人设」和「拼出的语气指令」，入队与生成分别在 bot.run_once /
bot._ai_reply 里接。

边界：这是【辩论/吵架】不是【辱骂】——无论人设怎么改，RED_LINE 都会强制追加，禁止
脏话、人身攻击、威胁、歧视，只在观点与逻辑上强硬反击。
"""
import json
import os
from pathlib import Path

import config

_STORE = Path(config.WORK_DIR) / 'battle_mode.json'

# 可在 UI 里编辑的默认「吵架人设」（语气/风格）。
DEFAULT_PERSONA = (
    "现在是辩论/吵架场景，对方在和你争论或挑衅。你要寸步不让、据理力争：\n"
    "- 逐点反驳对方的观点，抓住其逻辑漏洞、双标、前后矛盾，用事实和推理把话怼回去；\n"
    "- 语气强硬、自信、犀利，可以阴阳怪气、反问、反将一军，气势上完全压住对方，"
    "绝不示弱、不轻易认输、不打圆场；\n"
    "- 简短有力，一两句戳中要害，别长篇大论；对方每说一句你都接住并回击。"
)

# 始终强制追加、不可通过人设绕过的红线。
RED_LINE = (
    "\n【红线·不可越】只在【观点和逻辑】上开火，绝不做人身攻击：不准骂脏话、不准侮辱"
    "对方的长相/身材/家人/身份/地域/性别/疾病，不准威胁恐吓、不准歧视。靠道理和嘴皮子"
    "赢，不靠脏字。越过这条线就不是吵赢而是丢人。"
)

_HEADER = "\n\n【战斗模式·最高优先】"


def _read():
    try:
        data = json.loads(_STORE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STORE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, _STORE)


def _key():
    try:
        return config.account_key() or 'default'
    except Exception:
        return 'default'


def _slot(data, key=None):
    """取某账号的记录；兼容旧格式（值曾是 chats 列表）。"""
    key = key or _key()
    cur = data.get(key)
    if isinstance(cur, list):          # 旧格式迁移
        cur = {'chats': cur, 'persona': ''}
        data[key] = cur
    elif not isinstance(cur, dict):
        cur = {'chats': [], 'persona': ''}
        data[key] = cur
    cur.setdefault('chats', [])
    cur.setdefault('persona', '')
    return cur


def enable(chat):
    data = _read()
    slot = _slot(data)
    if chat not in slot['chats']:
        slot['chats'].append(chat)
        _write(data)
    return True


def disable(chat):
    data = _read()
    slot = _slot(data)
    if chat in slot['chats']:
        slot['chats'].remove(chat)
        _write(data)
        return True
    return False


def is_on(chat):
    return chat in _slot(_read())['chats']


def active_chats():
    """当前账号下所有开启战斗模式的会话（供 run_once 纳入轮询集合）。"""
    return list(_slot(_read())['chats'])


def get_persona():
    """当前账号的吵架人设正文（空则返回默认）。"""
    return _slot(_read())['persona'].strip() or DEFAULT_PERSONA


def is_custom_persona():
    return bool(_slot(_read())['persona'].strip())


def set_persona(text):
    data = _read()
    slot = _slot(data)
    slot['persona'] = (text or '').strip()[:4000]
    _write(data)
    return slot['persona']


def system_text():
    """拼给模型的完整语气指令：头 + 可配置人设 + 强制红线。"""
    return _HEADER + get_persona() + RED_LINE
