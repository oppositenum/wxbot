"""管理员聊天命令：会话内 `/命令` 由已配置管理员触发系统功能。

仅当发送者在 bot_rules.json 的 `admins` 名单内、且消息文字以 `/` 开头时才处理；
非管理员或普通消息一律不拦截（也不向对方暴露命令存在）。每条命令都把执行回执
发回该会话；失败也如实回报，绝不谎报成功。

新增命令只需在 COMMANDS 注册表里加一项 (handler, 帮助文本)。handler 签名：
    handler(args: str, ctx: dict) -> str   # 返回中文回执
ctx = dict(chat, m, is_group, rules, log)。
"""
import io
import re

from core import account_session as sessions


def is_admin(wxid, rules):
    """该发送者是否为已配置管理员（微信号 / 备注 / wxid 都能对上）。

    当前登录的本账号始终算管理员，否则群里自己发 /关闭战斗模式 会被丢掉。
    """
    if not wxid:
        return False
    try:
        import config
        if wxid == config.wxid():
            return True
    except Exception:
        pass
    listed = [a for a in ((rules or {}).get('admins') or []) if a]
    if not listed:
        return False
    if wxid in listed:
        return True
    want = {str(a).strip().casefold() for a in listed if str(a).strip()}
    needle = str(wxid).strip().casefold()
    if needle in want:
        return True
    try:
        from core import contacts
        for c in contacts.list_contacts():
            fields = {
                str(c.get(k) or "").strip().casefold()
                for k in ("username", "alias", "remark", "name", "nick_name")
            }
            if not (want & fields):
                continue
            if needle in fields or c.get("username") == wxid:
                return True
    except Exception:
        pass
    return False


# 群里发命令常带前导 @机器人（含微信的   分隔），识别前先剥掉。
_LEAD_AT = re.compile(r'^\s*(?:@[^\s ]+[\s ]+)+')


def command_text(content):
    """剥掉前导 @提及后的正文；用于判断/解析 `/命令`。"""
    return _LEAD_AT.sub('', content or '').lstrip()


def looks_command(content):
    return command_text(content).startswith('/')


# 管理员用口语问某个会话最近在聊什么。必须同时有总结意图和会话名字。
_SUMMARY_INTENT = ('总结', '聊聊', '聊了什么', '聊什么', '在聊', '说了什么', '说什么', '聊的什么')


def summary_request(content):
    """从管理员口语里取出要总结的会话名。不是这种请求就返回空。"""
    text = command_text(content)
    if not text or text.startswith('/'):
        return ''
    if not any(mark in text for mark in _SUMMARY_INTENT):
        return ''
    patterns = (
        r'(?:帮我|给我|跟我)?总结一下\s*(.+?)\s*(?:最近)?(?:和你|跟你|与你)?(?:聊了什么|聊什么|说了什么|说什么|在聊什么|的聊天|的情况)?$',
        r'(.+?)\s*最近\s*(?:和你|跟你|与你)?\s*(?:聊了什么|聊什么|说了什么|说什么|在聊什么)',
        r'(?:最近)?(?:和|跟|与)\s*(.+?)\s*(?:聊了什么|聊什么|说了什么|说什么|在聊什么)',
        r'总结一下\s*(.+)$',
    )
    for pattern in patterns:
        found = re.search(pattern, text)
        if not found:
            continue
        name = re.sub(r'^(帮我|给我|跟我|最近)', '', found.group(1).strip()).strip(' ，,。！？?')
        if name and name not in ('你', '我', '他', '她'):
            return name[:40]
    return ''


_SEND_VERBS = r'(?:发|说|告诉|转告|讲)'


def _plain_name(text):
    """去掉表情和英文尾巴，只留汉字。蜜蜜🌱besos 用来对「告诉蜜蜜」。"""
    return ''.join(ch for ch in (text or '') if '\u4e00' <= ch <= '\u9fff')


def _known_names():
    """通讯录和群里能用来对上「告诉蜜蜜…」的名字，长的优先。"""
    from core import contacts
    names = []
    for row in list(contacts.list_contacts()) + list(contacts.list_groups()):
        for key in ('remark', 'name', 'nick_name', 'alias'):
            value = (row.get(key) or '').strip()
            if len(value) >= 2:
                names.append(value)
    names.sort(key=len, reverse=True)
    return names


def send_request(content, names=None):
    """从管理员口语里取出代发目标。不是代发就返回空。

    names 是通讯录里的显示名。有它时，「告诉蜜蜜早点睡觉」能对上「蜜蜜🌱besos」。
    """
    text = command_text(content)
    if not text or text.startswith('/') or summary_request(text):
        return ''
    # 人设回复里的「告诉你」「发给你」不是代发。只认开头就是指令的短句。
    if len(text) > 80 or not re.match(
            r'^(?:帮我|给我|跟我)?(?:把|将)?(?:这张|刚才的?)?(?:图|图片|照片)?\s*(?:告诉|转告|发给|给|跟|向)',
            text):
        return ''
    if names is None:
        try:
            names = _known_names()
        except Exception:
            names = []
    glued = re.match(r'(?:帮我|给我|跟我)?(?:告诉|转告)\s*(.+)', text)
    if glued:
        blob = glued.group(1).strip()
        for name in names or []:
            plain = _plain_name(name)
            if len(plain) < 2 or not blob.startswith(plain):
                continue
            if len(blob) == len(plain):
                continue
            body = blob[len(plain):].strip().strip('：:，,')
            if body and plain not in ('你', '我', '他', '她'):
                return name + '\n' + body
    image_only = re.match(
        r'(?:帮我|给我|跟我)?(?:把|将)?(?:这张|刚才的?)?(?:图|图片|照片)\s*(?:发给|给)\s*(.+)$', text)
    if not image_only:
        image_only = re.match(r'(?:帮我|给我|跟我)?发给\s*(.+)$', text)
    if image_only:
        who = image_only.group(1).strip().strip(' ，,。！？')
        who = re.sub(r'(这张|刚才的)?(图|图片|照片)$', '', who).strip(' ，,。')
        matched = ''
        for name in names or []:
            plain = _plain_name(name)
            if len(plain) >= 2 and (who == plain or who.startswith(plain) or plain.startswith(who)):
                matched = name
                break
        who = matched or who
        if who and who not in ('你', '我', '他', '她'):
            return who + '\n'
    patterns = (
        rf'(?:帮我|给我|跟我)?(?:告诉|转告)\s*(.+?)\s*[：:，,\s]\s*(.+)',
        rf'(?:帮我|给我|跟我)?(?:给|跟|向)\s*(.+?)\s*{_SEND_VERBS}\s*(?:一?[句条个])?\s*(?:消息|信息)?\s*[：:，,\s]?\s*(.+)',
        rf'(?:帮我|给我|跟我)?{_SEND_VERBS}\s*(?:一?[句条个])?\s*(?:消息|信息)?\s*给\s*(.+?)\s*[：:，,\s]\s*(.+)',
    )
    for pattern in patterns:
        found = re.search(pattern, text)
        if not found:
            continue
        who = found.group(1).strip().strip(' ，,。')
        body = found.group(2).strip()
        who = re.sub(r'^(帮我|给我|跟我)', '', who).strip()
        if who and body and who not in ('你', '我', '他', '她') and len(who) <= 40:
            return who + '\n' + body
    return ''


# ---------- 各命令处理器 ----------

def _cmd_moment(args, ctx):
    """/朋友圈 <内容>：发一条文字朋友圈；内容留空则自动生成（按设置可能配图）。"""
    from core import moments, moments_jobs, moments_ai
    text = (args or '').strip()
    assets = []
    if not text:
        post = moments_ai.generate_post(moments.settings().get('moods') or [])
        text = post['text']
        if post.get('image_prompt'):
            aid = moments_jobs.make_publish_image(post['image_prompt'])
            if aid:
                assets = [aid]
    draft = moments.save_draft(dict(kind='publish', text=text, assets=assets))
    job = moments_jobs.enqueue_draft(draft['id'], draft['revision'])
    pic = '（含配图）' if assets else ''
    return f"✅ 已排队发布朋友圈{pic}（{job['state']}）：\n{text}"


def _cmd_moment_image(args, ctx):
    """/配图朋友圈 <主题>：主题交大模型润色成正文，再按内容生成配图后发图文朋友圈。"""
    from core import moments, moments_jobs, moments_ai, llm
    topic = (args or '').strip()
    if not topic:
        return '⚠️ 用法：/配图朋友圈 主题内容（会由大模型润色成正文并按内容配图）'
    caps = moments.capabilities()
    if not caps.get('image_publish'):
        return '⚠️ 当前环境不支持生成配图：' + (caps.get('reason') or '未知原因')
    post = moments_ai.compose_from_topic(topic)          # 主题 → 润色正文 + 配图画面描述
    text = post['text']
    try:
        data = llm.gen_image(post['image_prompt'], cfg=llm.load_cfg())
    except Exception as exc:  # noqa: BLE001
        return '⚠️ 配图生成失败：' + (str(exc) or type(exc).__name__)[:80]
    if not data:
        return '⚠️ 配图生成失败：模型未返回图片'
    aid = moments.upload(io.BytesIO(data))['id']
    draft = moments.save_draft(dict(kind='publish', text=text, assets=[aid]))
    job = moments_jobs.enqueue_draft(draft['id'], draft['revision'])
    return f"✅ 已排队发布图文朋友圈（{job['state']}）：\n{text}"


def _cmd_sync(args, ctx):
    """/同步朋友圈：立即刷新并同步朋友圈缓存。"""
    from core import moments_jobs
    r = moments_jobs.refresh() or {}
    return (f"✅ 已同步朋友圈：解析 {r.get('parsed', 0)} 条，"
            f"新增 {r.get('added', 0)}，更新 {r.get('changed', 0)}")


def _cmd_status(args, ctx):
    """/朋友圈状态：查看同步与自动化状态。"""
    from core import moments
    s = moments.status()
    v = moments.settings()
    last = (s.get('last_sync') or {}).get('at') if isinstance(s.get('last_sync'), dict) else s.get('last_sync')
    when = '从未' if not last else _ago(last)
    autos = '、'.join(k for k, on in [('自动评论', v.get('auto_comment')),
                                       ('自动发布', v.get('auto_publish')),
                                       ('聊天反思', v.get('chat_reflection'))] if on) or '全部关闭'
    return (f"📊 朋友圈状态\n缓存动态：{s.get('count', 0)} 条\n上次同步：{when}\n"
            f"自动化：{autos}\n后台运行：{'是' if s.get('automation_running') else '否'}")


# 友好别名 → moments 设置里的布尔开关键
_SETTING_ALIASES = {
    '自动评论': 'auto_comment', '自动发布': 'auto_publish', '聊天反思': 'chat_reflection',
    '配图': 'publish_images', '网络观点': 'publish_web_opinions', '同步': 'sync_enabled',
}
_ON = {'开', 'on', '1', 'true', '是', '开启', '打开'}
_OFF = {'关', 'off', '0', 'false', '否', '关闭'}


def _cmd_set(args, ctx):
    """/设置 <项> <开|关>：开关一项朋友圈设置。"""
    from core import moments
    parts = (args or '').split()
    if len(parts) < 2:
        return '⚠️ 用法：/设置 <项> <开|关>；项可为：' + '、'.join(_SETTING_ALIASES)
    name, val = parts[0], parts[1].lower()
    key = _SETTING_ALIASES.get(name)
    if not key:
        return '⚠️ 未知设置项：' + name + '；可用：' + '、'.join(_SETTING_ALIASES)
    if val in _ON:
        on = True
    elif val in _OFF:
        on = False
    else:
        return '⚠️ 值只能是 开 或 关'
    current = moments.settings()
    moments.save_settings({key: on}, current['revision'])
    return f"✅ 已{'开启' if on else '关闭'}{name}"


def _recent_image(chat, before_id, within=600):
    """管理员这条指令之前、十分钟内的最近一张图。"""
    import time
    from core import messages
    now = time.time()
    rows = messages.get_messages(chat, limit=20) or []
    for m in reversed(rows):
        if (m.get('local_id') or 0) >= (before_id or 0):
            continue
        if m.get('type') != 3:
            continue
        if now - (m.get('create_time') or now) > within:
            continue
        return m
    return None


def _save_relay_image(chat, message):
    import os
    from core import imgdec
    data, mime = imgdec.get_msg_image(chat, message.get('local_id'))
    if not data:
        return None, mime or '找不到这张图'
    ext = '.png' if 'png' in (mime or '') else '.jpg'
    folder = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'wxbot-relay')
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{message.get('local_id')}{ext}")
    with open(path, 'wb') as f:
        f.write(data)
    return path, None


def _cmd_send(args, ctx):
    """/发 <联系人或群> <内容>：代发文字。内容为空时转发最近一张图。"""
    from core import sendq
    parts = (args or '').split(None, 1)
    if not parts or not parts[0].strip():
        return '⚠️ 用法：/发 <联系人或群> <内容>'
    who = parts[0].strip()
    content = parts[1].strip() if len(parts) > 1 else ''
    chat, info = _resolve_chat(who)
    if not chat:
        if info == 'none':
            return f'⚠️ 未找到会话「{who}」'
        return f'⚠️「{who}」匹配到多个（{info[6:]}），请用更精确的名字'
    if content:
        sendq.enqueue('text', info, chat=chat, content=content)
        return f"✅ 已发送给 {info}：{content[:40]}"
    source = ctx.get('chat')
    image = _recent_image(source, (ctx.get('m') or {}).get('local_id'))
    if not image:
        return '⚠️ 没有找到刚才的图片。先发一张图，再说发给谁。'
    path, err = _save_relay_image(source, image)
    if not path:
        return '⚠️ 这张图还发不了：' + (err or '未解密')
    sendq.enqueue('image', info, chat=chat, path=path)
    return f"✅ 已把刚才的图片发给 {info}"


def _resolve_chat(name):
    """把名字解析成会话 username（群或联系人）；唯一命中才返回 (username, 显示名)。

    未命中返回 (None, 'none')，多个命中返回 (None, 'multi:名字、名字…')。
    """
    from core import contacts
    name = (name or '').strip()
    if not name:
        return None, 'none'
    hits = {}
    for g in contacts.list_groups():
        if name in (g.get('name') or '') or name == g.get('username'):
            hits[g.get('username')] = g.get('name') or g.get('username')
    for c in contacts.list_contacts():
        if name == c.get('username') or any(
                name in (c.get(k) or '') for k in ('remark', 'name', 'nick_name', 'alias')):
            hits[c.get('username')] = c.get('remark') or c.get('name') or c.get('username')
    if not hits:
        return None, 'none'
    # 「蜜蜜」会同时命中「蜜蜜」和「蜜蜜🌱besos」。一个名字包含另一个时算同一个会话。
    values = list(hits.values())
    if len(hits) > 1 and not all(
            any(a != b and (a in b or b in a) for b in values) for a in values):
        return None, 'multi:' + '、'.join(values[:5])
    u = next(iter(hits))
    return u, hits[u]


def _battle_target(args, ctx):
    """战斗命令的目标：给了名字就解析，否则=当前会话。返回 (chat, 显示名, 错误串或None)。"""
    from core import bot
    target = (args or '').strip()
    if not target:
        chat = ctx['chat']
        return chat, bot.send_name_for(chat), None
    chat, info = _resolve_chat(target)
    if chat:
        return chat, info, None
    if info == 'none':
        return None, None, f'⚠️ 未找到会话「{target}」'
    return None, None, f'⚠️「{target}」匹配到多个（{info[6:]}…），请用更精确的名字'


def _cmd_battle_on(args, ctx):
    """/开启战斗模式 [会话]：对该会话逐条自动回击、据理力争（不@也回）。默认当前会话。"""
    from core import battle_mode
    chat, disp, err = _battle_target(args, ctx)
    if err:
        return err
    from core import continuous_mode
    continuous_mode.disable(chat)
    battle_mode.enable(chat)
    return (f"⚔️ 已对「{disp}」开启战斗模式：这个会话的每条消息都会自动回击、据理力争，"
            "不用@也回。只在观点上开火，不会爆粗口/人身攻击。收兵发 /关闭战斗模式。")


def _cmd_battle_off(args, ctx):
    """/关闭战斗模式 [会话]：收兵，停止对该会话的自动回击。默认当前会话。"""
    from core import battle_mode
    chat, disp, err = _battle_target(args, ctx)
    if err:
        return err
    battle_mode.disable(chat)
    return f"🕊️ 已对「{disp}」关闭战斗模式，收兵。"


def _cmd_continuous_on(args, ctx):
    """/开启持续模式 [会话]：该会话不用 @ 也逐条接话（玩接龙等）。默认当前会话。"""
    from core import continuous_mode
    chat, disp, err = _battle_target(args, ctx)
    if err:
        return err
    continuous_mode.enable(chat)
    return (f"🔁 已对「{disp}」开启持续模式：不用@也会接话，适合玩成语接龙。"
            f"{continuous_mode.IDLE_OFF // 60} 分钟没人说话自动关闭，手动关发 /关闭持续模式。")


def _cmd_continuous_off(args, ctx):
    """/关闭持续模式 [会话]：恢复为 @/引用才回。默认当前会话。"""
    from core import continuous_mode
    chat, disp, err = _battle_target(args, ctx)
    if err:
        return err
    continuous_mode.disable(chat)
    return f"⏸️ 已对「{disp}」关闭持续模式，恢复 @ 我才回。"


def _cmd_help(args, ctx):
    """/帮助：列出全部命令。"""
    return '可用命令：\n' + _help_text()


def _cmd_summary(args, ctx):
    """/总结 <群或联系人>：把该会话最近的聊天总结后回给管理员。"""
    import time
    from core import llm, messages
    target = (args or '').strip()
    if not target:
        return '⚠️ 用法：/总结 <群名或联系人>  例如 /总结 白榆'
    chat, info = _resolve_chat(target)
    if not chat:
        if info == 'none':
            return f'⚠️ 未找到会话「{target}」'
        return f'⚠️「{target}」匹配到多个（{info[6:]}），请用更精确的名字'
    rows = messages.get_messages(chat, limit=40) or []
    lines = []
    now = time.time()
    for m in rows:
        if m.get('type') not in (1, 49):
            continue
        body = (m.get('content') or '').strip()
        if not body:
            continue
        who = '我' if m.get('is_self') else (m.get('sender_name') or '对方')
        age = _ago(m.get('create_time') or now)
        lines.append(f'{age} {who}: {body[:180]}')
    if not lines:
        return f'「{info}」最近没有可总结的文字消息。'
    transcript = '\n'.join(lines[-30:])
    prompt = (f'下面是微信会话「{info}」最近的消息，时间从旧到新。'
              '用几句口语总结：谁在说、主要在聊什么、有没有没说完的事。'
              '只根据这些消息，不要补设定，不要评价。')
    try:
        text = (llm.chat(prompt, [{'role': 'user', 'content': transcript}]) or '').strip()
    except Exception as exc:  # noqa: BLE001
        return '⚠️ 总结失败：' + (str(exc) or type(exc).__name__)[:80]
    if not text:
        return f'⚠️「{info}」这次没有总结出来。'
    return f'「{info}」最近：\n{text}'


# ---------- 注册表与分发 ----------

COMMANDS = {
    '朋友圈': (_cmd_moment, '/朋友圈 <内容>  发一条朋友圈（内容留空=自动生成）'),
    '配图朋友圈': (_cmd_moment_image, '/配图朋友圈 <主题>  主题交大模型润色成正文并按内容配图，发图文朋友圈'),
    '同步朋友圈': (_cmd_sync, '/同步朋友圈  立即同步朋友圈'),
    '朋友圈状态': (_cmd_status, '/朋友圈状态  查看同步与自动化状态'),
    '设置': (_cmd_set, '/设置 <项> <开|关>  项：自动评论/自动发布/聊天反思/配图/网络观点/同步'),
    '发': (_cmd_send, '/发 <联系人或群> <内容>  代发一条消息'),
    '开启战斗模式': (_cmd_battle_on, '/开启战斗模式 [会话]  对该会话逐条自动回击、据理力争（默认当前会话）'),
    '关闭战斗模式': (_cmd_battle_off, '/关闭战斗模式 [会话]  收兵，停止自动回击（默认当前会话）'),
    '开启持续模式': (_cmd_continuous_on, '/开启持续模式 [会话]  不用@也逐条接话，玩接龙用（默认当前会话，1小时无人说话自动关）'),
    '关闭持续模式': (_cmd_continuous_off, '/关闭持续模式 [会话]  恢复@我才回（默认当前会话）'),
    '帮助': (_cmd_help, '/帮助  查看全部命令'),
    '总结': (_cmd_summary, '/总结 <群名或联系人>  把该会话最近的聊天总结后回给管理员'),
}


def _help_text():
    return '\n'.join(h for _, h in COMMANDS.values())


def _ago(ts):
    import time
    d = time.time() - ts
    if d < 90:
        return '刚刚'
    if d < 3600:
        return f'{int(d // 60)}分钟前'
    if d < 86400:
        return f'{int(d // 3600)}小时前'
    return f'{int(d // 86400)}天前'


def _send_receipt(chat, m, msg, log):
    from core import bot, sender, send_ledger
    try:
        sender.send_text(bot.send_name_for(chat), msg, chat_username=chat,
                         job_id=send_ledger.stable_id(sessions.capture()['account'],
                                                      'admin_cmd', chat, m.get('local_id')))
    except Exception as exc:  # noqa: BLE001
        log(f"[管理员命令] 回执发送失败: {exc}")


@sessions.task
def dispatch(chat, m, is_group, rules, log=print):
    """解析并执行一条管理员命令。返回是否已处理（处理了就跳过普通闲聊回复）。

    调用前应已确认发送者是管理员（见 is_admin）。这里只负责解析 `/命令`、执行、发回执。
    """
    text = command_text(m.get('content'))
    asked = summary_request(text)
    if asked:
        text = '/总结 ' + asked
    else:
        relay = send_request(text, _known_names())
        if relay:
            who, body = relay.split('\n', 1)
            text = '/发 ' + who + (' ' + body if body else '')
    if not text.startswith('/'):
        return False
    body = text[1:].strip()
    if not body:
        return False
    mo = re.match(r'(\S+)\s*(.*)', body, re.S)
    name, args = mo.group(1), mo.group(2)
    entry = COMMANDS.get(name)
    if not entry:
        _send_receipt(chat, m, '未知命令：/' + name + '\n' + _help_text(), log)
        return True
    try:
        msg = entry[0](args, dict(chat=chat, m=m, is_group=is_group, rules=rules, log=log))
    except Exception as exc:  # noqa: BLE001
        log(f"[管理员命令] {name} 执行出错: {exc}")
        msg = '⚠️ 执行失败：' + (str(exc) or type(exc).__name__)
    _send_receipt(chat, m, msg, log)
    log(f"[管理员命令] {chat}: /{name} => {(msg or '').splitlines()[0][:40]}")
    return True
