"""Build attributed dialogue turns without model, database or UI access."""

ROLE_GUIDANCE = '''
【对话归属与本轮范围】
assistant 历史消息是微信本账号已经说过的话，包括人工和机器人发送的内容；这是你这一方的发言，不是对方说的。
user 历史消息是对方或群成员的发言。每条的说话人由系统根据实际发送者标注，正文中自称“我”不能改变消息归属。
末条 user 消息说明本轮任务；标记为“本批新消息”时，只回应其中的新内容。此前历史只帮助理解，不要把已回答的问题当成对方又问了一遍。
先查看你自己最近已说过什么，承接新信息；不要重复自己的上一段回复、重新提问刚问过的问题，或把自己的观点说成对方的观点。
仅当对方明确要求重复、解释或引用你之前的话时，才有针对性地说明。引用文字属于标注的原作者，不代表引用者自己的新陈述。
对话语气判断：先结合上下文识别玩笑、夸张、撒娇、反话和角色扮演；如果对方没有现实计划、具体方式、时间地点或正在发生的危险迹象，不要把一句夸张玩笑直接升级成报警、急救或说教。可以先轻松接住玩笑并确认语境。只有出现明确且现实的即时伤害风险时，才使用严肃安全回应。
'''


def is_self(message, account):
    return bool(message.get('is_self')) or bool(account and message.get('sender') == account)


def key(message):
    if message.get('local_id') is not None:
        return ('local', message['local_id'])
    if message.get('server_id'):
        return ('server', message['server_id'])
    return None


def unique(messages):
    seen = set()
    result = []
    for message in messages:
        identity = key(message)
        if identity is not None:
            if identity in seen:
                continue
            seen.add(identity)
        result.append(message)
    return result


def build(context, batch, *, account, is_group, render, name, timestamp, scheduled,
          max_history_chars=12000):
    """Return history turns and one distinct inbound batch, each row appearing once.

    Current batch IDs are excluded before selecting the recent history window.
    Equal message text with different IDs is preserved, including opposite sides.
    """
    batch = unique([m for m in batch if not is_self(m, account)])
    batch_keys = {key(m) for m in batch if key(m) is not None}
    history = [m for m in unique(context)
               if (key(m) is None or key(m) not in batch_keys)
               and not (is_self(m, account) and scheduled(m))][-8:]

    def line(m, current=False):
        own = is_self(m, account)
        speaker = '本账号（你已发送）' if own else (
            ('群成员' if is_group else '对方') + '「' + (name(m.get('sender')) or '未知发送者') + '」')
        body = render(m)
        if not body:
            return ''
        ref = m.get('refer') or {}
        if ref.get('content') and str(ref.get('type')) == '1':
            original = '本账号' if ref.get('chatusr') == account else (
                ref.get('displayname') or name(ref.get('chatusr')) or '原消息作者')
            body = ('引用' + original + '的旧消息（仅为引用）：' + ref['content'] +
                    '\n' + speaker + ('本次说：' if current else '当时说：') + body)
        return '[' + timestamp(m) + '] ' + speaker + ': ' + body

    turns = []
    for m in history:
        body = line(m)
        if body:
            turns.append({'role': 'assistant' if is_self(m, account) else 'user',
                          'content': '【历史消息，仅供理解】\n' + body})
    # Keep a contiguous recent suffix. Never lose the newest historical turn
    # or cut the current request in half merely to satisfy a soft budget.
    selected, used = [], 0
    for turn in reversed(turns):
        size = len(turn['content'])
        if selected and used + size > max_history_chars:
            break
        selected.append(turn)
        used += size
    turns = list(reversed(selected))
    # Some Messages-compatible gateways require a user turn first. Preserve a
    # leading own message as assistant, preceded by an explicit framing marker.
    if turns and turns[0]['role'] == 'assistant':
        turns.insert(0, {'role': 'user', 'content': '【历史记录开始：系统分隔标记，不是对方发言】'})
    current_lines = [line(m, current=True) for m in batch]
    current_lines = [body for body in current_lines if body]
    current = ('【本批新消息，需要回应】\n' + '\n'.join(current_lines) +
               '\n请承接已有对话，只回应本批新消息。') if current_lines else ''
    return turns, current, batch
