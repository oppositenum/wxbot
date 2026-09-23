"""战斗（吵架/辩论）模式：对指定会话逐条回应、据理力争，不看是否 @/引用。

由管理员命令 /开启战斗模式、/关闭战斗模式 控制（见 admin_commands），也可在 UI 里
配置专属人设。状态与人设按账号隔离、落盘持久化，进程重启后仍生效。这里只管「哪些
会话开着」「吵架人设」和「拼出的语气指令」，入队与生成分别在 bot.run_once /
bot._ai_reply 里接。

边界：这是【辩论/吵架】不是小作文。输出格式由 FORMAT_LOCK 强制追加，人设改不掉。
"""
import json
import os
from pathlib import Path

import config


def _legacy_store():
    return Path(config.WORK_DIR) / 'battle_mode.json'


def _store():
    """按账号落在 accounts/<wxid>/，随 accounts 挂载持久化。"""
    return Path(config.account_dir()) / 'battle_mode.json'

# 可在 UI 里编辑的默认「吵架人设」（语气/风格）。
DEFAULT_PERSONA = (
    "现在是吵架现场，对方在抬杠或挑衅。你要寸步不让、短促回击：\n"
    "- 抓住对方这句话的漏洞、双标、前后矛盾，一针见血怼回去；\n"
    "- 语气硬、阴阳、反问都可以，气势压住对方，不认输、不打圆场、不讲大道理；\n"
    "- 每条微信极短，像真人连戳几下，不要写成一段完整议论文。"
)

# 无论人设怎么改，都按这个格式出字。
FORMAT_LOCK = (
    "\n\n【战斗输出·强制】\n"
    "- 每条不超过 25 个字，一句一个点，口语，不要标点堆砌成段。\n"
    "- 需要连击就输出 2～4 条，条与条用单独一行 [[NEXT]] 分隔；每条单独发一条微信。\n"
    "- 禁止长篇、禁止分点列举、禁止小作文、禁止复述对方原话再分析。\n"
    "- 只输出要发出去的正文，不要旁白。"
)

RED_LINE = FORMAT_LOCK

_HEADER = "\n\n【战斗模式·最高优先】"


def _empty():
    return {'chats': [], 'persona': '', 'grok_model': '', 'grok_reasoning_effort': ''}


def _normalize(cur):
    if isinstance(cur, list):
        return {'chats': list(cur), 'persona': ''}
    if not isinstance(cur, dict):
        return _empty()
    chats = cur.get('chats')
    if not isinstance(chats, list):
        chats = []
    return {
        'chats': list(chats),
        'persona': cur.get('persona') or '',
        'grok_model': (cur.get('grok_model') or '').strip(),
        'grok_reasoning_effort': (cur.get('grok_reasoning_effort') or '').strip().lower(),
    }


def _legacy_slot():
    """旧文件是 work/battle_mode.json，按账号 key 分槽。"""
    try:
        data = json.loads(_legacy_store().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    key = config.account_key() or 'default'
    if key in data:
        return _normalize(data.get(key))
    # 整文件已经是单账号 {chats, persona}
    if 'chats' in data or 'persona' in data:
        return _normalize(data)
    return None


def _read():
    path = _store()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return _normalize(data)
    except (OSError, ValueError):
        pass
    migrated = _legacy_slot()
    if migrated:
        _write(migrated)
        return migrated
    return _empty()


def _write(data):
    path = _store()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _normalize(data)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def _slot(data=None, key=None):
    """当前账号的记录。"""
    return data if data is not None else _read()


def enable(chat):
    slot = _read()
    if chat not in slot['chats']:
        slot['chats'].append(chat)
        _write(slot)
    return True


def disable(chat):
    slot = _read()
    if chat in slot['chats']:
        slot['chats'].remove(chat)
        _write(slot)
        return True
    return False


def is_on(chat):
    return chat in _read()['chats']


def active_chats():
    """当前账号下所有开启战斗模式的会话（供 run_once 纳入轮询集合）。"""
    return list(_read()['chats'])


def get_persona():
    """当前账号的吵架人设正文（空则返回默认）。"""
    return _read()['persona'].strip() or DEFAULT_PERSONA


def is_custom_persona():
    return bool(_read()['persona'].strip())


def set_persona(text):
    slot = _read()
    slot['persona'] = (text or '').strip()[:4000]
    _write(slot)
    return slot['persona']


def get_grok_model():
    return _read()['grok_model']


def get_grok_reasoning():
    return _read()['grok_reasoning_effort']


def set_grok_route(model, reasoning=''):
    """战斗模式单独用的 Grok 模型；空模型=跟随主配置。"""
    from core import llm
    slot = _read()
    slot['grok_model'] = (model or '').strip()[:200]
    effort = (reasoning or '').strip().lower()
    if effort and effort not in llm.REASONING_EFFORTS:
        raise ValueError('不支持的 reasoning：' + effort)
    slot['grok_reasoning_effort'] = effort
    _write(slot)
    return {'grok_model': slot['grok_model'], 'grok_reasoning_effort': slot['grok_reasoning_effort']}


def chat_cfg(base=None):
    """吵架时覆盖主配置：强制 grok + 本页模型/reasoning。未填模型则返回 None。"""
    from core import llm
    model = get_grok_model()
    if not model:
        return None
    cfg = dict(base if base is not None else llm.load_cfg())
    grok = dict(cfg.get('grok') or {})
    grok['model'] = model
    cfg['grok'] = grok
    cfg['provider'] = 'grok'
    cfg['no_gpt_fallback'] = True
    cfg['single_attempt'] = True
    effort = get_grok_reasoning()
    if effort:
        cfg['grok_reasoning_effort'] = effort
    return cfg


def system_text():
    """拼给模型的完整语气指令：头 + 可配置人设 + 强制短句连发。"""
    return _HEADER + get_persona() + FORMAT_LOCK
