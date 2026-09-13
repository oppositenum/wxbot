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
    """该 wxid 是否为已配置管理员。"""
    return bool(wxid) and wxid in set((rules or {}).get('admins') or [])


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


def _cmd_send(args, ctx):
    """/发 <联系人> <内容>：代发一条文字消息给某位联系人。"""
    from core import contacts, sendq
    parts = (args or '').split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        return '⚠️ 用法：/发 <联系人> <内容>'
    who, content = parts[0], parts[1].strip()
    people = contacts.list_contacts()
    exact = [c for c in people if who in (c.get('remark'), c.get('name'),
                                          c.get('nick_name'), c.get('alias'), c.get('username'))]
    matches = exact or [c for c in people
                        if any(who in (c.get(k) or '') for k in ('remark', 'name', 'nick_name', 'alias'))]
    if not matches:
        return f'⚠️ 未找到联系人「{who}」'
    if len(matches) > 1:
        names = '、'.join((c.get('name') or c.get('username')) for c in matches[:5])
        return f'⚠️ 「{who}」匹配到多个联系人（{names}…），请用更精确的名字'
    t = matches[0]
    display = t.get('name') or t.get('username')
    sendq.enqueue('text', display, chat=t['username'], content=content)
    return f"✅ 已发送给 {display}：{content[:40]}"


def _cmd_help(args, ctx):
    """/帮助：列出全部命令。"""
    return '可用命令：\n' + _help_text()


# ---------- 注册表与分发 ----------

COMMANDS = {
    '朋友圈': (_cmd_moment, '/朋友圈 <内容>  发一条朋友圈（内容留空=自动生成）'),
    '配图朋友圈': (_cmd_moment_image, '/配图朋友圈 <主题>  主题交大模型润色成正文并按内容配图，发图文朋友圈'),
    '同步朋友圈': (_cmd_sync, '/同步朋友圈  立即同步朋友圈'),
    '朋友圈状态': (_cmd_status, '/朋友圈状态  查看同步与自动化状态'),
    '设置': (_cmd_set, '/设置 <项> <开|关>  项：自动评论/自动发布/聊天反思/配图/网络观点/同步'),
    '发': (_cmd_send, '/发 <联系人> <内容>  代发一条消息给联系人'),
    '帮助': (_cmd_help, '/帮助  查看全部命令'),
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
    text = (m.get('content') or '').lstrip()
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
