"""持续模式：对指定会话（主要是群）不用 @/引用，逐条接话，用正常人设。

典型场景：群里用机器人玩成语接龙、猜谜、接歌词，每次都 @ 太麻烦。
由管理员命令 /开启持续模式、/关闭持续模式 控制（见 admin_commands）。
与战斗模式互斥；按账号隔离、落盘持久化。

防刷屏：会话连续 IDLE_OFF 秒没有别人说话就自动关闭；模型可输出 SKIP_TOKEN
表示这条不是对它说的、不插话。
"""
import json
import os
import re
import time
from pathlib import Path

import config

IDLE_OFF = 3600          # 无人说话 1 小时自动关闭
SKIP_TOKEN = "[[SKIP]]"

PROMPT = (
    "\n\n【持续模式·群聊】本群开着持续模式：群友不用 @ 你，你会看到群里每一条消息。\n"
    "- 是否在玩游戏、轮到谁、该接哪个字，以下面【接龙判定】为准，不要自己从聊天里猜。\n"
    "- 聊天就是聊天：抱怨、吐槽、问问题、四字感叹（气死我了、笑死我了），都按角色正常接话，不要拿来接龙。\n"
    "- 不要解释规则、不要问「现在谁接」「要不要换一个」、不要复述自己的思考过程（例如「X开头，重接」）。\n"
    "- 明显是群友之间在聊、不是对你说的，就只输出 " + SKIP_TOKEN + "。\n"
    "- 回复要短，像群里真人随手回一句；只发一条，不要用 [[NEXT]] 连发。"
)


def _store():
    return Path(config.account_dir()) / 'continuous_mode.json'


def _read():
    try:
        data = json.loads(_store().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    chats = data.get('chats') if isinstance(data, dict) else None
    return {k: v for k, v in (chats or {}).items() if isinstance(v, dict)}


def _write(chats):
    path = _store()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps({'chats': chats}, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def enable(chat, now=None):
    now = time.time() if now is None else now
    chats = _read()
    chats[chat] = {'since': now, 'last': now}
    _write(chats)
    from core import battle_mode
    battle_mode.disable(chat)
    return True


def disable(chat):
    chats = _read()
    if chat in chats:
        chats.pop(chat)
        _write(chats)
        return True
    return False


def touch(chat, now=None):
    """有人说话，刷新空闲计时。"""
    chats = _read()
    if chat in chats:
        chats[chat]['last'] = time.time() if now is None else now
        _write(chats)


def expire(now=None):
    """关掉空闲超时的会话，返回被关掉的会话列表。"""
    now = time.time() if now is None else now
    chats = _read()
    dead = [c for c, v in chats.items() if now - (v.get('last') or v.get('since') or 0) > IDLE_OFF]
    if dead:
        for c in dead:
            chats.pop(c)
        _write(chats)
    return dead


def is_on(chat):
    return bool(chat) and chat in _read()


def active_chats():
    return list(_read())


def is_skip(text):
    return SKIP_TOKEN in (text or "")


# ---------- 成语接龙：接哪个字、用过哪些，由代码算好，不交给模型猜 ----------

_IDIOM = re.compile(r'^[一-鿿]{4}$')
_GAME_WINDOW = 1800      # 只看最近半小时的接龙


def idiom_of(msg):
    """消息正文恰好是一个四字词（去掉 @、标点、空白）就返回它。"""
    if msg.get('type') not in (1, None):
        return ''
    text = re.sub(r'@\S+', '', msg.get('content') or '')
    text = re.sub(r'[\s 　，。！？!?,.~～…、"“”]', '', text)
    return text if _IDIOM.match(text) else ''


_START = re.compile(r'(玩|来|开始|继续|开).{0,4}(成语)?接龙|成语接龙')
_STOP = re.compile(r'不玩了|不玩啦|不接了|结束|别玩了|不想玩|你自己玩')


def _readings(ch):
    """一个字的全部读音（去声调）；没装 pypinyin 就只认同一个字。"""
    try:
        from pypinyin import pinyin, Style
        return set(pinyin(ch, style=Style.NORMAL, heteronym=True)[0]) | {ch}
    except Exception:
        return {ch}


def pinyin_of(ch):
    try:
        from pypinyin import lazy_pinyin, Style
        return lazy_pinyin(ch, style=Style.TONE)[0]
    except Exception:
        return ''


def links(prev, word):
    """word 能否接在 prev 后面：同一个字，或同音字。"""
    if not prev or not word:
        return False
    a, b = prev[-1], word[0]
    return a == b or bool(_readings(a) & _readings(b))


def idiom_game(rows, now=None):
    """从最近消息里还原接龙局面。没在玩返回 None。

    判定「在玩」要有明确信号，不把普通四字聊天当出题：
    - 有人说要玩接龙，之后出现的四字成语开局；或
    - 连续两个四字词首尾接得上（同字或同音），自然开局。
    接不上的四字话（「气死我了」「我不玩了」）当聊天，不进链；有人说不玩了/结束就散局。

    返回 dict：last=链上最新成语，need=该用哪个字开头，mine=最新那个是不是本账号出的
    （是的话就轮到对方），used=本局已出现。
    """
    now = time.time() if now is None else now
    chain, pending, started = [], None, False
    for m in sorted(rows, key=lambda x: x.get('local_id') or 0):
        if m.get('create_time') and now - m['create_time'] > _GAME_WINDOW:
            continue
        text = m.get('content') or ''
        if text.startswith('/') or '持续模式' in text or '战斗模式' in text:
            continue                  # 管理命令与机器人的开关回执，不是游戏内容
        if m.get('is_self') and _HINT.search(text):
            # 本账号刚提示过对方「不是成语/没接上」：那一步作废，仍轮到对方。
            if chain and not chain[-1][1]:
                chain.pop()
            pending = None
            continue
        if m.get('type') in (1, None) and _STOP.search(text):
            chain, pending, started = [], None, False
            continue
        word = idiom_of(m)
        if not word:
            if m.get('type') in (1, None) and _START.search(text):
                started, chain, pending = True, [], None
            continue
        entry = (word, bool(m.get('is_self')))
        used = {w for w, _ in chain}
        if chain and links(chain[-1][0], word) and word not in used:
            chain.append(entry)
            pending = None
        elif not chain and started:
            chain, pending = [entry], None
        elif pending and links(pending[0], word) and word != pending[0]:
            chain, pending = [pending, entry], None
        else:
            pending = entry
    if not chain:
        return None
    last, mine = chain[-1]
    # 轮到对方时，对方在我之后发的、没接上的四字话：可能是接错了，也可能只是聊天，交给模型判断。
    attempt = pending[0] if (mine and pending and not pending[1]) else ''
    prev = chain[-2][0] if len(chain) > 1 else ''
    return {'last': last, 'need': last[-1], 'need_py': pinyin_of(last[-1]), 'mine': mine,
            'prev': prev, 'attempt': attempt,
            'used': list(dict.fromkeys(w for w, _ in chain))}


NOT_IDIOM = "[[NOT_IDIOM]]"
# 提示语由代码生成，固定句式，idiom_game 靠它识别「那一步作废」。
_HINT = re.compile(r'^「[^」]{1,8}」好像不是成语|^要用「.」')


def hint(game):
    """对方接的不算数时，发给对方的提示。"""
    if game['mine']:
        py = f"（{game['need_py']}）" if game['need_py'] else ''
        if game['attempt'] in game['used']:
            return f"要用「{game['need']}」{py}开头哦，「{game['attempt']}」前面用过啦～"
        return f"要用「{game['need']}」{py}开头哦，再想想～"
    if game['prev']:
        ch = game['prev'][-1]
        return f"「{game['last']}」好像不是成语哦，换个「{ch}」开头的试试～"
    return f"「{game['last']}」好像不是成语哦，换一个试试～"


COMMON = "优先用大家都熟悉的常见成语，常见的实在接不上才用生僻的，别一上来就出很难的把人难住。"


NO_GAME = (f"\n\n【接龙判定·代码已算好】现在没有进行中的成语接龙。群友发的四字话是在聊天，不是出题，"
           f"不要拿它接龙；除非有人明确说要玩接龙。是聊天就按角色正常接话，不是对你说的就输出 {SKIP_TOKEN}。")


def game_prompt(game):
    if not game:
        return NO_GAME
    used = '、'.join(game['used'][-30:])
    if game['mine']:
        head = (f"\n\n【接龙判定·代码已算好】链上最新的是你出的「{game['last']}」，现在轮到对方"
                f"用「{game['need']}」开头。别自己接自己。")
        if game['attempt']:
            return head + (f"对方刚发了「{game['attempt']}」，没接上「{game['need']}」。"
                           f"如果他是在接龙（接错了字，或者不是成语），只输出 {NOT_IDIOM}；"
                           f"如果只是在聊天，就按角色正常回一句。")
        return head + f"对方在跟你说话就正常回一句，否则只输出 {SKIP_TOKEN}。"
    py = f"（{game['need_py']}，同音字也行）" if game['need_py'] else ''
    return (f"\n\n【接龙判定·代码已算好，必须照做】对方刚出「{game['last']}」，轮到你。"
            f"先判断「{game['last']}」是不是真实存在的成语：不是的话只输出 {NOT_IDIOM}，不要接它。"
            f"是成语的话，你的成语必须以「{game['need']}」{py}开头，优先同一个字。{COMMON}"
            f"本局已出现、不能再用：{used}。只输出一个真实的四字成语，不要连发，不要加任何别的话。"
            f"真的接不上就大方认输，一句话就行。")


def check_answer(game, text):
    """该出成语时，模型输出必须是接得上、没用过的四字成语（或认输/判对方不是成语）。其余情况不拦。"""
    if not game or game['mine']:
        return True
    if NOT_IDIOM in (text or '') or '认输' in (text or '') or '接不上' in (text or ''):
        return True
    word = idiom_of({'type': 1, 'content': text})
    return bool(word) and links(game['last'], word) and word not in game['used']
