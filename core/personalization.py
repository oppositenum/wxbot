"""Account-local contact settings, evidence-backed communication preferences and jobs.

No facts or dialogue copies live here: facts remain in memory.py. SQLite commits
settings, evidence progress and audit together. No model/network/UI calls.
"""
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid

import config
from core import account_session as sessions, distill

FIELDS = {
    'length': ['未知', '简短', '适中', '详细'],
    'tone': ['未知', '直接', '温和', '轻松', '正式'],
    'emoji': ['未知', '少量', '较多', '不喜欢'],
    'jokes': ['未知', '接受', '不喜欢'],
    'advice': ['未知', '先听对方说', '直接给方案', '视情况'],
    'followup': ['未知', '少追问', '愿意展开'],
    'address': None, 'language': None, 'dislikes': None,
}
LABELS = dict(zip(FIELDS, ['回复长度', '表达方式', '表情符号', '玩笑', '建议方式', '追问', '称呼', '语言', '反感表达']))
BEHAVIOR = ('【通用要求】真实、切题。对话里你就是当前角色本人：对方问你是不是角色名时，用角色口吻半开玩笑承认'
            '（例如「我是啊，还能是谁」），禁止说自己是AI、在扮演、只是氛围、或真身另有其人。'
            '不要把角色经历说成这个微信账号的现实履历，也不要编造对方的现实身份、工作、家庭或共同经历；'
            '当前角色允许的调情、亲密称呼、主动推进和虚构场景，按角色正文执行，不要用客服腔改写成礼貌助手。'
            '当前明确请求决定本轮任务；交流偏好只辅助表达，不能盖过角色正文。'
            '旧机器人回复只供理解对话，不是当前角色指令。事实记忆不因角色切换失效。'
            '不报告画像标签。对方明确结束或喊停时尊重其意愿。')
DEFAULT_PERSONA = {'name': '内置助手', 'persona': '你是一个友善、诚实、尊重边界的聊天助手。', 'samples': []}


class Conflict(ValueError):
    pass


def _id(contact):
    if not isinstance(contact, str) or not contact or len(contact) > 200 or any(c in contact for c in '/\\\x00'):
        raise ValueError('需要稳定联系人 ID')
    return contact


def _path():
    return os.path.join(config.account_dir(), 'communication.sqlite3')


@contextlib.contextmanager
def _db(write=False):
    token = sessions.check()
    path = _path()
    if not write and not os.path.exists(path):
        yield None
        return
    con = sqlite3.connect(path if write else 'file:' + path + '?mode=ro', uri=not write, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        if write:
            con.execute('PRAGMA synchronous=FULL')
            con.executescript('''
                CREATE TABLE IF NOT EXISTS conversation_state (chat TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS analysis_jobs (id TEXT PRIMARY KEY, contact TEXT, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS analysis_batches (job TEXT, ordinal INTEGER, status TEXT, attempts INTEGER, data TEXT NOT NULL, PRIMARY KEY(job,ordinal));
                CREATE TABLE IF NOT EXISTS analysis_items (id TEXT PRIMARY KEY, job TEXT, contact TEXT, status TEXT, signature TEXT, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS analysis_rejected (contact TEXT, signature TEXT, PRIMARY KEY(contact,signature));
                CREATE TABLE IF NOT EXISTS analysis_applications (id TEXT PRIMARY KEY, contact TEXT, revision INTEGER, undone INTEGER, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (contact TEXT PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, contact TEXT, revision INTEGER, ts INTEGER, source TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS seen (contact TEXT, msg_id TEXT, PRIMARY KEY(contact,msg_id));
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, contact TEXT, lo INTEGER, hi INTEGER, cursor INTEGER, total INTEGER, scanned INTEGER, status TEXT, kind TEXT NOT NULL DEFAULT 'history', attempts INTEGER DEFAULT 0, error TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS progress (contact TEXT PRIMARY KEY, cursor INTEGER NOT NULL);
            ''')
            con.execute('BEGIN IMMEDIATE')
        yield con
        sessions.check(token)
        if write:
            # Serialize with observed epoch changes through the commit boundary.
            with sessions._lock:
                sessions.check(token)
                con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _default():
    return dict(persona_id=None, personalization_enabled=False, auto_update=False,
                conversation_control_enabled=False, agent_enabled=True,
                skip_closing_replies=True, shared_sessions=False,
                preferences={}, revision=0)


def _get(con, contact):
    row = con.execute('SELECT revision,data FROM settings WHERE contact=?', (contact,)).fetchone() if con else None
    if not row:
        if con and not contact.startswith('__'):
            template = _get(con, '__default_template__')
            if template.get('template_enabled') and not template.get('config_error') and _template_target(contact):
                return _template_values(template, contact) | {'revision': 0, 'inherited_template': template['revision']}
        return _default()
    try:
        data = json.loads(row['data'])
        if not isinstance(data, dict) or not isinstance(data.get('preferences', {}), dict):
            raise ValueError()
        if any(not isinstance(data.get(k, False), bool) for k in
               ('auto_update', 'personalization_enabled', 'conversation_control_enabled')):
            raise ValueError()
        if 'agent_enabled' in data and not isinstance(data.get('agent_enabled'), bool):
            raise ValueError()
        if 'skip_closing_replies' in data and not isinstance(data.get('skip_closing_replies'), bool):
            raise ValueError()
        if 'shared_sessions' in data and not isinstance(data.get('shared_sessions'), bool):
            raise ValueError()
        _slug(data.get('persona_id'))
        for field, pref in data.get('preferences', {}).items():
            if (field not in FIELDS or not isinstance(pref, dict) or not isinstance(pref.get('value'), str)
                    or pref.get('source') not in ('admin_manual', 'user_explicit', 'inferred')
                    or not isinstance(pref.get('evidence_ids'), list)):
                raise ValueError()
        # Old automatic ingestion also stored the old True defaults. Only a
        # recorded administrator save establishes opt-in for those legacy rows.
        # This is a read-time interpretation; do not rewrite real contact data.
        if 'configured_features' not in data:
            manual = con.execute("SELECT 1 FROM audit WHERE contact=? AND source='admin_manual' LIMIT 1",
                                 (contact,)).fetchone()
            if not manual:
                data['personalization_enabled'] = False
                data['auto_update'] = False
        return dict(_default(), **dict(data, revision=row['revision']))
    except (ValueError, TypeError):
        return dict(_default(), revision=row['revision'], config_error='配置损坏，使用内置回退')


def _put(con, contact, data, source):
    data = dict(data)
    data.pop('inherited_template', None)
    value = dict(data, revision=data['revision'] + 1)
    raw = json.dumps(value, ensure_ascii=False)
    con.execute('INSERT OR REPLACE INTO settings VALUES(?,?,?)', (contact, value['revision'], raw))
    con.execute('INSERT INTO audit(contact,revision,ts,source,data) VALUES(?,?,?,?,?)',
                (contact, value['revision'], int(time.time()), source, raw))
    return value


def _template_target(contact):
    from core import contacts
    if contact.endswith(('@chatroom', '@openim')) or contact.startswith(('__', 'gh_')):
        return False
    if contact in ('filehelper', 'weixin', 'newsapp', 'notifymessage', 'fmessage', 'medianote', 'qqmail', 'tmessage', 'qmessage'):
        return False
    return any(c['username'] == contact for c in contacts.list_contacts()) and contact != config.wxid()


def _template_values(template, contact):
    data = _default()
    for key in ('persona_id', 'personalization_enabled', 'auto_update',
                'conversation_control_enabled', 'agent_enabled', 'skip_closing_replies',
                'shared_sessions'):
        data[key] = template.get(key, data[key])
    data['configured_features'] = ['personalization_enabled', 'auto_update', 'conversation_control_enabled']
    data['preferences'] = {k: dict(value=v['value'], locked=v.get('locked', False),
        source='admin_manual', confidence=1.0, evidence_ids=[], updated=template.get('template_updated', int(time.time())),
        scope='chat:' + contact) for k, v in template.get('preferences', {}).items()}
    return data


@sessions.task
def save_template(source, revision, enabled=True):
    if not _template_target(source) or not isinstance(enabled, bool):
        raise ValueError('需要当前账号的私聊联系人')
    with _db(True) as con:
        old = _get(con, '__default_template__')
        if old['revision'] != revision:
            raise Conflict('模板已变化，请刷新后重试')
        original = _get(con, source)
        if original.get('config_error'):
            raise ValueError('来源配置不可读')
        role = resolve_persona(source)
        if role.get('error'):
            raise ValueError('来源人设不可用')
        data = _template_values(original, '__default_template__')
        data.update(persona_id=role['persona_id'], revision=revision, template_enabled=enabled,
                    template_source=source, template_updated=int(time.time()))
        return _put(con, '__default_template__', data, 'admin_manual')


@sessions.task
def apply_template(contact, revision, template_revision):
    if not _template_target(contact):
        raise ValueError('模板只适用于当前账号的私聊联系人')
    with _db(True) as con:
        template = _get(con, '__default_template__')
        current = _get(con, contact)
        if not template['revision'] or template.get('config_error'):
            raise ValueError('请先保存默认模板')
        if template['revision'] != template_revision or current['revision'] != revision:
            raise Conflict('联系人或模板已变化，请刷新后重试')
        data = dict(current, **_template_values(template, contact))
        data['revision'] = current['revision']
        return _put(con, contact, data, 'admin_manual')


@sessions.task
def get(contact):
    _id(contact)
    try:
        with _db() as con:
            return _get(con, contact)
    except sqlite3.Error:
        return dict(_default(), config_error='交流配置存储不可读，使用内置回退')


def _slug(slug):
    if slug is not None and (not isinstance(slug, str) or not slug or len(slug) > 200 or '/' in slug or '\\' in slug or '\x00' in slug or slug in ('.', '..')):
        raise ValueError('无效人设 ID')
    return slug


@sessions.task
def update(contact, patch, revision):
    _id(contact)
    if set(patch) - {'persona_id', 'personalization_enabled', 'auto_update',
                     'conversation_control_enabled', 'agent_enabled',
                     'skip_closing_replies', 'shared_sessions', 'preferences',
                     '_persona_ready'}:
        raise ValueError('未知设置字段')
    with _db(True) as con:
        data = _get(con, contact)
        if data['revision'] != revision:
            raise Conflict('配置已被修改，请刷新后重试')
        data.pop('config_error', None)
        for key in ('persona_id', 'personalization_enabled', 'auto_update',
                    'conversation_control_enabled', 'agent_enabled', 'skip_closing_replies',
                    'shared_sessions'):
            if key in patch:
                v = patch[key]
                if key == 'persona_id':
                    _slug(v)
                    if v and not _persona_exists(v) and not patch.get('_persona_ready'):
                        raise ValueError('所选人设不存在')
                elif not isinstance(v, bool):
                    raise ValueError('开关必须是布尔值')
                data[key] = v
        if data.get('shared_sessions') and contact.endswith('@chatroom'):
            raise ValueError('群聊不支持会话共享')
        configured = set(data.get('configured_features', []))
        configured.update(k for k in patch if k in
                          ('personalization_enabled', 'auto_update', 'conversation_control_enabled'))
        data['configured_features'] = sorted(configured)
        for field, p in patch.get('preferences', {}).items():
            if field not in FIELDS:
                raise ValueError('未知交流偏好')
            if p is None:
                data['preferences'].pop(field, None)
                continue
            value = p.get('value')
            if not isinstance(value, str) or not value.strip() or len(value) > 160 or (FIELDS[field] and value not in FIELDS[field]):
                raise ValueError('无效偏好值')
            if not isinstance(p.get('locked', False), bool):
                raise ValueError('锁定必须是布尔值')
            data['preferences'][field] = dict(value=value, locked=p.get('locked', False), evidence_ids=[],
                source='admin_manual', confidence=1.0, updated=int(time.time()), scope='chat:' + contact)
        if contact.endswith('@chatroom') and patch.get('preferences'):
            raise ValueError('本轮不维护群成员交流偏好')
        return _put(con, contact, data, 'admin_manual')


def legacy_inventory(rules):
    entries = [dict(index=i, name=r.get('name', ''), persona_id=r.get('action', {}).get('persona') or None,
                    match=r.get('match', {})) for i, r in enumerate((rules or {}).get('rules', []))
               if r.get('action', {}).get('type') == 'reply_ai']
    ids = {e['persona_id'] for e in entries}
    return dict(entries=entries, conflict=len(ids) > 1, unique=next(iter(ids)) if len(ids) == 1 else None)


@sessions.task
def resolve_persona(contact, rules=None, rule=None):
    """Contact override wins. Legacy mode remains explicit until admin migration.

    Conflicting legacy rules are retained only before migration, with exact triggering
    rule required for replies. The historical first rule for proactive entries is
    reported as such; UI never claims one role when rule-dependent.
    """
    _id(contact)
    if rules is None:
        from core.bot import load_rules
        rules = load_rules()
    data, global_ = get(contact), get('__global__')
    inv = legacy_inventory(rules)
    error = data.get('config_error') or global_.get('config_error')
    slug = data.get('persona_id')
    source = 'contact' if slug else 'global'
    if not slug:
        if global_['revision']:
            slug = global_.get('persona_id')
        elif inv['entries']:
            selected = rule if rule is not None else (rules or {}).get('rules', [])[inv['entries'][0]['index']]
            slug = selected.get('action', {}).get('persona')
            source = 'legacy_rule_conflict' if inv['conflict'] else 'legacy_global'
            # Before explicit migration the triggering legacy rule remains
            # authoritative, including groups. Do not silently replace old roles.

    p = None
    try:
        _slug(slug)
        p = distill.load_persona(slug) if slug and not error else None
        if p and (not isinstance(p, dict) or not isinstance(p.get('persona'), str) or not p['persona'].strip()):
            p = None
    except (OSError, ValueError, TypeError):
        error = '人设文件不可读或配置损坏'
    if p is None:
        error = error or ('引用人设不存在或内容无效' if slug else '')
        p = dict(DEFAULT_PERSONA)
        source = 'fallback' if error else 'builtin'
    p = dict(p, name=p.get('name') or '机器人角色')
    return dict(persona=p, persona_id=slug, source=source, error=error or '',
                legacy=inv, managed=bool(global_['revision']))


_SWITCH_INTENT = re.compile(
    r'换|切到|切成|当成|当回|还是当|变成|改成|换成|扮演|人设|用一下|给我当|做回|变回')
_ADULT_CUES = re.compile(
    r'想做了|想要了|来做[啊呀吧]?|开荤|想色色|想做爱|想被操|想上床|进入状态|开车|'
    r'黄一点|骚一点|成人模式|想浪|想被干|做爱')
_ADULT_PREF = ('成人3', '角色甲成人', '成人2', '成人', '成人男友', '成人女友')


def catalog():
    rows = []
    try:
        rows = distill.list_personas() or []
    except Exception:
        rows = []
    out = []
    for row in rows:
        slug = row.get('slug') or ''
        name = row.get('name') or slug
        aliases = {slug, name}
        if slug.endswith('成人') and len(slug) > 2:
            aliases.add(slug[:-2] + '的成人')
            aliases.add('成人' + slug[:-2])
        out.append({'slug': slug, 'name': name, 'aliases': aliases})
    return out


def match_switch(text, current=None):
    """对方点名人设则切；没点名但要开荤且当前不是成人向，切到可用的成人人设。"""
    t = (text or '').replace(' ', '')
    if not t:
        return None
    pairs = []
    for item in catalog():
        for alias in item['aliases']:
            if alias and len(alias) >= 2:
                pairs.append((alias, item['slug'], item['name']))
    pairs.sort(key=lambda x: -len(x[0]))
    named = None
    if _SWITCH_INTENT.search(t):
        for alias, slug, name in pairs:
            if alias in t:
                named = slug
                break
    if named and named != current:
        return named
    if named == current:
        return None
    if current and '成人' in (current or ''):
        return None
    if not _ADULT_CUES.search(t):
        return None
    have = {item['slug'] for item in catalog()}
    for slug in _ADULT_PREF:
        if slug in have and slug != current:
            return slug
    return None


_ROLE_PASTE = re.compile(r'扮演《[^》]{1,40}》中的([^，,。\s]{1,12})')


def pasted_role(text):
    """对方发来的整份角色说明书。普通聊天里的「扮演」不算。"""
    raw = (text or '').strip()
    if len(raw) < 400:
        return None
    found = _ROLE_PASTE.search(raw.replace(' ', ''))
    if not found or '人格' not in raw:
        return None
    name = found.group(1).strip('，,。 ')
    if not name or len(name) > 12:
        return None
    return name


@sessions.task
def adopt_pasted_role(contact, text, *, is_group=None):
    """把发来的整份人设存成当前会话的人设。群也只改这个群，不改别人的私聊。"""
    if is_group is None:
        is_group = bool(contact and str(contact).endswith('@chatroom'))
    name = pasted_role(text)
    if not contact or not name:
        return None
    body = text.strip()
    slug = name
    existing = distill.load_persona(slug)
    if existing and (existing.get('persona') or '').strip() != body:
        slug = name + '-贴入'
    distill.import_persona(name, body, slug=slug)
    data = get(contact)
    if data.get('persona_id') != slug:
        try:
            update(contact, {'persona_id': slug, '_persona_ready': True}, data['revision'])
        except Conflict:
            data = get(contact)
            if data.get('persona_id') != slug:
                update(contact, {'persona_id': slug, '_persona_ready': True}, data['revision'])
    return {'slug': slug, 'name': name,
            'persona': {'slug': slug, 'name': name, 'persona': body, 'samples': []}}


@sessions.task
def maybe_switch(contact, text, *, is_group=None):
    """按这句切换专属人设。成功返回 {slug,name,persona}，否则 None。

    只改私聊绑定。群里即使正在回某个私聊对象，也不能把她的私聊人设切走或套进来。
    """
    if is_group is None:
        is_group = bool(contact and str(contact).endswith('@chatroom'))
    if not contact or is_group:
        return None
    data = get(contact)
    current = data.get('persona_id')
    slug = match_switch(text, current)
    if not slug or slug == current or not _persona_exists(slug):
        return None
    try:
        update(contact, {'persona_id': slug}, data['revision'])
    except Conflict:
        data = get(contact)
        if data.get('persona_id') == slug:
            pass
        else:
            update(contact, {'persona_id': slug}, data['revision'])
    p = distill.load_persona(slug)
    if not p:
        return None
    return {'slug': slug, 'name': p.get('name') or slug, 'persona': p}


def _persona_exists(slug):
    if not slug:
        return False
    try:
        p = distill.load_persona(slug)
        return isinstance(p, dict) and isinstance(p.get('persona'), str) and bool(p['persona'].strip())
    except (OSError, ValueError, TypeError):
        return False


@sessions.task
def detach_persona(slug):
    """Drop bindings to a deleted persona. Does not restore another role.

    Contact/global/template records become inherit/unset; reply rules and
    schedule tasks lose the explicit slug. Reply then uses remaining global,
    leftover rules, or the built-in assistant — never a missing file.
    """
    slug = _slug(slug)
    if not slug:
        return dict(contacts=0, global_=False, template=False, rules=0, schedules=0)
    released = dict(contacts=0, global_=False, template=False, rules=0, schedules=0)
    if os.path.exists(_path()):
        with _db(True) as con:
            rows = con.execute('SELECT contact,revision,data FROM settings').fetchall()
            for row in rows:
                contact = row['contact']
                try:
                    stored = json.loads(row['data'])
                except (ValueError, TypeError):
                    continue
                if not isinstance(stored, dict) or stored.get('persona_id') != slug:
                    continue
                data = _get(con, contact)
                if data.get('config_error'):
                    continue
                data['persona_id'] = None
                _put(con, contact, data, 'persona_deleted')
                if contact == '__global__':
                    released['global_'] = True
                elif contact == '__default_template__':
                    released['template'] = True
                else:
                    released['contacts'] += 1
    from core import bot
    rules_path = bot.rules_file()
    if os.path.exists(rules_path):
        with bot.RULES_LOCK:
            with open(rules_path, encoding='utf-8') as fp:
                rules = json.load(fp)
            changed = 0
            for rule in rules.get('rules') or []:
                action = rule.get('action') if isinstance(rule, dict) else None
                if isinstance(action, dict) and action.get('type') == 'reply_ai' and action.get('persona') == slug:
                    action['persona'] = ''
                    changed += 1
            if changed:
                bot.save_rules(rules)
                released['rules'] = changed
    from core import schedule
    try:
        tasks = schedule.load_tasks()
    except Exception:
        tasks = []
    sched = 0
    for task in tasks:
        if isinstance(task, dict) and task.get('persona') == slug:
            task['persona'] = None
            sched += 1
    if sched:
        schedule.save_tasks(tasks)
        released['schedules'] = sched
    return released


@sessions.task
def set_global(slug, revision, rules):
    """Explicit, auditable migration. Conflicts require manual rule reconciliation."""
    _slug(slug)
    if slug and not _persona_exists(slug):
        raise ValueError('所选人设不存在')
    inv = legacy_inventory(rules)
    current = get('__global__')
    if current.get('config_error'):
        raise Conflict('全局存储损坏，需要恢复备份，不能自动迁移')
    if not current['revision'] and inv['conflict']:
        raise Conflict('旧规则人设冲突，请先逐项核对；不能自动迁移')
    if (not current['revision'] and inv['unique'] and slug != inv['unique']
            and _persona_exists(inv['unique'])):
        raise Conflict('首次迁移必须保留当前唯一规则人设；完成迁移后可更改全局')
    with _db(True) as con:
        data = _get(con, '__global__')
        if data['revision'] != revision:
            raise Conflict('配置已被修改，请刷新后重试')
        first = not data['revision']
        if first:
            data['migration'] = dict(source='legacy_rules', rules=inv['entries'], updated=int(time.time()))
        data['persona_id'] = slug
        return _put(con, '__global__', data, 'legacy_migration' if first else 'admin_global')


def _eligible(contact, m):
    return (not contact.endswith('@chatroom') and m.get('chat') == contact and m.get('sender') == contact
            and m.get('is_self') is False and m.get('type') == 1
            and m.get('origin', 'user') == 'user' and not m.get('scheduled') and not m.get('system')
            and not m.get('refer') and not m.get('fictional') and bool(m.get('server_id') or m.get('local_id')))


def extract(contact, msg):
    """Conservative durable explicit feedback only; no guessed personality/inference."""
    if not _eligible(contact, msg):
        return {}
    text = (msg.get('content') or '').strip()
    # Reject quotes, questions, mixed/conditional feedback and one-off requests.
    if (len(text) > 120 or re.search(r'[“”「」"？?]|这次|这回|今天|现在|暂时|如果|假如|比如|他说|她说', text)
            or not re.search(r'以后|今后|一直|每次', text)):
        return {}
    patterns = {
        'length': [(r'(回复|回答|说)?(尽量)?短一点|简短一点', '简短'), (r'(回复|回答|说)(尽量)?详细一点', '详细')],
        'tone': [(r'(说话|表达|回复)直接一点', '直接'), (r'(说话|表达|回复)温和一点', '温和'), (r'(说话|表达|回复)正式一点', '正式')],
        'emoji': [(r'(不要|别)用表情', '不喜欢'), (r'少用(一点)?表情', '少量'), (r'多用(一点)?表情', '较多')],
        'jokes': [(r'(不要|别)(跟我)?开玩笑', '不喜欢')],
        'advice': [(r'先听我说', '先听对方说'), (r'直接给(我)?方案', '直接给方案')],
        'followup': [(r'(少|不要|别)(总是|一直)?追问', '少追问')],
        'dislikes': [(r'(不要|别)(跟我)?说教', '说教'), (r'(不要|别)重复安慰', '重复安慰')],
    }
    durable = re.fullmatch(r'(?:以后|今后|每次)(?:请|麻烦)?(?:你)?(?:都)?(.+?)[。！!，,\s]*', text)
    if not durable:
        return {}
    feedback = durable.group(1)
    found = {}
    for field, choices in patterns.items():
        values = {v for pattern, v in choices if re.fullmatch(pattern, feedback)}
        if len(values) == 1:
            found[field] = values.pop()
    for field, pattern in [('address', r'以后(?:请)?叫我([^，。！\s]{1,16})(?:[。！]|$)'),
                           ('language', r'以后(?:请)?用(中文|英文|英语|粤语|普通话)(?:回复|回答|跟我说)')]:
        m = re.search(pattern, text)
        if m:
            found[field] = m.group(1)
    return found


def _ingest(con, contact, rows):
    data = _get(con, contact)
    if not data['auto_update'] or data.get('config_error'):
        return 0
    changed = 0
    for m in rows:
        if not _eligible(contact, m) or not extract(contact, m):
            continue
        mid = str(m.get('server_id') or ('local:' + str(m['local_id'])))
        if con.execute('SELECT 1 FROM seen WHERE contact=? AND msg_id=?', (contact, mid)).fetchone():
            continue
        con.execute('INSERT INTO seen VALUES(?,?)', (contact, mid))
        for field, value in extract(contact, m).items():
            old = data['preferences'].get(field, {})
            stamp = int(m.get('create_time') or 0)
            if old.get('locked') or (old.get('source') in ('user_explicit', 'admin_manual') and old.get('evidence_time', old.get('updated', 0)) > stamp):
                continue
            data['preferences'][field] = dict(value=value, evidence_ids=[mid], source='user_explicit',
                confidence=1.0, updated=int(time.time()), evidence_time=stamp, locked=False, scope='chat:' + contact)
            changed += 1
    if changed:
        _put(con, contact, data, 'user_explicit')
    return changed


@sessions.task
def learn_live(contact, rows):
    """Durable live batch intent, then atomic evidence application.

    A failed/interrupted batch stays in the same management job list for explicit
    resume from original DB IDs; advancing the bot reply cursor cannot erase it.
    No dialogue bodies are copied to the job database.
    """
    _id(contact)
    if contact.endswith('@chatroom') or not get(contact)['auto_update']:
        return 0
    bound = [dict(m, chat=m.get('chat', contact)) for m in rows]
    eligible = [m for m in bound if _eligible(contact, m) and extract(contact, m)]
    if not eligible:
        return 0
    ids = [int(m['local_id']) for m in bound if m.get('local_id')]
    if not ids:
        return 0
    lo, hi = min(ids) - 1, max(ids)
    jid = 'live-' + hashlib.sha256((contact + ':' + str(lo) + ':' + str(hi)).encode()).hexdigest()[:32]
    with _db(True) as con:
        con.execute("INSERT OR IGNORE INTO jobs(id,contact,lo,hi,cursor,total,scanned,status,kind) VALUES(?,?,?,?,?,?,0,'ready','live')",
                    (jid, contact, lo, hi, lo, len(bound)))
        prior = con.execute('SELECT status FROM jobs WHERE id=?', (jid,)).fetchone()
        if prior['status'] == 'done':
            return 0
    try:
        with _db(True) as con:
            changed = _ingest(con, contact, eligible)
            con.execute("UPDATE jobs SET cursor=hi,scanned=total,status='done',error='' WHERE id=?", (jid,))
            return changed
    except sessions.StaleAccount:
        raise
    except Exception as exc:
        with _db(True) as con:
            con.execute("UPDATE jobs SET status='failed',attempts=attempts+1,error=? WHERE id=?", (type(exc).__name__, jid))
        raise


@sessions.task
def selected_preferences(contact, query=''):
    if contact.endswith('@chatroom'):
        return []
    data = get(contact)
    if not data['personalization_enabled'] or data.get('config_error'):
        return []
    selected = []
    for field, pref in sorted(data['preferences'].items(), key=lambda kv: (not kv[1].get('locked'), kv[1].get('source') == 'inferred')):
        if field not in FIELDS or pref.get('scope') != 'chat:' + contact or pref.get('value') == '未知':
            continue
        if field == 'length' and re.search(r'详细|展开|完整|逐步|简短|一句话|字以内', query):
            continue
        if field in ('jokes', 'address') and not re.search(r'玩笑|称呼|叫我|怎么叫', query):
            continue
        selected.append(dict(pref, field=field))
        if len(selected) >= 4:
            break
    return selected


@sessions.task
def selected_strategies(contact, query=''):
    if contact.endswith('@chatroom'):
        return []
    from core.profile_drafts import STRATEGIES, STRATEGY_FIELDS
    data = get(contact)
    if not data['personalization_enabled'] or data.get('config_error'):
        return []
    budget = 4 - len(selected_preferences(contact, query))
    chosen = []
    for field, entry in data.get('strategies', {}).items():
        if len(chosen) >= budget:
            break
        if (field not in STRATEGIES or not isinstance(entry, dict) or not entry.get('reviewed')
                or entry.get('value') not in STRATEGIES[field] or entry.get('scope') != 'chat:' + contact):
            continue
        if any(data['preferences'].get(k, {}).get('locked') for k in STRATEGY_FIELDS[field]):
            continue
        if field == 'structure' and re.search(r'详细|展开|完整|逐步|简短|一句话|字以内', query):
            continue
        chosen.append(dict(entry, field=field))
    return chosen


def _private_share_target(username):
    """Only ordinary private chats can join a share circle."""
    u = username or ''
    if not u or u.startswith('__') or u.endswith(('@chatroom', '@openim')) or u.startswith('gh_'):
        return False
    return u not in ('filehelper', 'weixin', 'newsapp', 'notifymessage', 'fmessage',
                     'medianote', 'qqmail', 'tmessage', 'qmessage')


@sessions.task
def shared_peers(contact, rules=None):
    """Other private contacts in the same opt-in share circle.

    Circle membership requires: both sides have shared_sessions on, both resolve
    to the same non-empty persona_id, and both are private chats. Default off
    keeps every session isolated.
    """
    _id(contact)
    if not _private_share_target(contact):
        return []
    mine = get(contact)
    if not mine.get('shared_sessions') or mine.get('config_error'):
        return []
    role = resolve_persona(contact, rules)
    slug = role.get('persona_id')
    if not slug or role.get('error'):
        return []
    from core import contacts
    peers = []
    for row in contacts.list_contacts():
        other = row.get('username') or ''
        if other == contact or not _private_share_target(other):
            continue
        data = get(other)
        if not data.get('shared_sessions') or data.get('config_error'):
            continue
        other_role = resolve_persona(other, rules)
        if other_role.get('persona_id') != slug or other_role.get('error'):
            continue
        peers.append({'username': other, 'name': row.get('name') or other,
                      'persona_id': slug})
    peers.sort(key=lambda x: x['name'])
    return peers


_SHARE_META_RE = re.compile(
    r'几个人|多少人|勾搭|暧昧|别人|其他[人女男]?|还有谁|和谁|跟谁|几个对象|外面|'
    r'查岗|自己查|查一查|翻.{0,6}记录|看看.{0,6}聊天|证据|交代|坦白')

# 「给小甲讲个笑话」「告诉小甲，给她讲个冷笑话」「告诉小乙 我想她了」——仅共享圈内可代发。
_RELAY_JOKE_RES = (
    re.compile(r'(?:告诉|转告)\s*(.+?)[，,、]?\s*给她?(?:讲|说|发)\s*(?:一?[个条句])?(?:冷)?笑话'),
    re.compile(r'(?:告诉|转告)\s*(.+?)\s*(?:讲|说|发)\s*(?:一?[个条句])?(?:冷)?笑话'),
    re.compile(r'(?:给|跟|向)\s*(.+?)\s*(?:讲|说|发)\s*(?:一?[个条句])?(?:冷)?笑话'),
)
_RELAY_TEXT_RES = (
    re.compile(r'帮我?(?:给|跟|向)\s*(.+?)\s*(?:说|发|讲)\s*[：:\s]*(.+)'),
    re.compile(r'(?:给|跟|向)\s*(.+?)\s*(?:发|说|讲)\s*(?:一?[个条句句])?\s*[：:\s]+(.+)'),
    re.compile(r'(?:告诉|转告)\s*(.+?)\s*[：:\s]+(.+)'),
    # 「告诉小乙 我想她了」(空格分隔，无冒号)
    re.compile(r'(?:告诉|转告)\s*([^\s，,、]{1,20})\s+(.+)'),
)
_RELAY_LOOKS_RE = re.compile(
    r'(?:给|跟|向|告诉|转告).{0,20}(?:讲|说|发).{0,6}(?:冷)?笑话|'
    r'(?:告诉|转告)\s*\S+|'
    r'(?:给|跟|向)\s*\S+.{0,8}(?:发|说|讲)')


def _match_shared_peer(whom, peers):
    """Longest name / short-alias match inside the shared circle."""
    whom = re.sub(r'^(?:你|帮我|给我)', '', (whom or '').strip())
    whom = whom.strip(' ，,、的')
    if not whom or whom in ('她', '他', '它', '对方'):
        return None
    hit, hit_len = None, 0
    whom_l = whom.lower()
    for peer in peers:
        name = (peer.get('name') or '').strip()
        if not name:
            continue
        name_l = name.lower()
        if whom_l == name_l or whom_l in name_l or name_l in whom_l:
            score = max(len(whom), len(name))
            if score > hit_len:
                hit, hit_len = peer, score
        short = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', name)
        if short and (whom == short or whom in short or short in whom):
            score = len(whom) + len(short)
            if score > hit_len:
                hit, hit_len = peer, score
    return hit


def parse_shared_relay(text, peers):
    """Parse a direct-send request aimed at one shared-circle peer.

    Returns dict(peer, kind, content) or None. kind is 'joke' or 'text'.
    Only matches peers already in the opt-in same-persona circle.
    """
    raw = (text or '').strip()
    if not raw or not peers:
        return None
    kind, whom, content = None, None, None
    for cre in _RELAY_JOKE_RES:
        m = cre.search(raw)
        if m:
            kind, whom, content = 'joke', m.group(1).strip(), None
            break
    if not kind:
        for cre in _RELAY_TEXT_RES:
            m = cre.search(raw)
            if m:
                kind, whom, content = 'text', m.group(1).strip(), (m.group(2) or '').strip()
                break
    if not kind or not whom:
        return None
    if kind == 'text' and not content:
        return None
    # Strip trailing「，给她」debris from「告诉小甲，给她」
    whom = re.sub(r'[，,、].*$', '', whom).strip()
    hit = _match_shared_peer(whom, peers)
    if not hit:
        return None
    return {'peer': hit, 'kind': kind, 'content': content, 'whom': whom}


def _relay_outbound(persona, peer_name, kind, content, requester_name=''):
    """Build the text that will be sent into the peer chat."""
    from core import llm
    role = (persona or {}).get('persona') or ''
    name = (persona or {}).get('name') or '我'
    if kind == 'joke':
        prompt = (
            f'你正在扮演「{name}」。{requester_name or "有人"}让你给「{peer_name}」讲个笑话。'
            '只输出笑话正文本身，口语、短一点，不要开场白，不要说这是受人所托。')
        out = (llm.chat(role + '\n' + prompt if role else prompt,
                        [{'role': 'user', 'content': f'给{peer_name}讲个笑话'}],
                        cfg={**llm.load_cfg(), 'max_tokens': 120, 'no_gpt_fallback': True})
               or '').strip()
        return out or f'{peer_name}，来听个冷的：为什么海鸥总在海上飞？因为海边有「岸」。'
    # Explicit text: send as the persona speaking, lightly framed if needed.
    body = (content or '').strip()
    if len(body) <= 80 and not re.search(r'[。！？.!?…]', body):
        # Short imperative like「晚上别熬夜」— keep bare so it feels natural.
        return body
    return body


@sessions.task
def try_shared_relay(contact, text, persona=None, rules=None):
    """If text asks to message a shared peer, send now and return current-chat reply.

    Direct-send policy: no confirmation step. Only same-persona shared peers.
    Returns None when this is not a relay request.
    """
    _id(contact)
    if contact.endswith('@chatroom') or not get(contact).get('shared_sessions'):
        return None
    peers = shared_peers(contact, rules)
    parsed = parse_shared_relay(text, peers)
    if not parsed:
        # Looks like a relay, but target is outside the circle / unmatched.
        # Never let the model fake「已经告诉她了」.
        if _RELAY_LOOKS_RE.search(text or ''):
            return ('共享圈里没有叫这个名字、或对方没开会话共享的人，'
                    '我只能给同人设且勾了共享的好友直接发。'
                    '（现在圈里有：' + '、'.join(p['name'] for p in peers[:8]) +
                    ('…' if len(peers) > 8 else '') + '）')
        return None
    peer = parsed['peer']
    from core import sender, contacts
    requester = ''
    try:
        for c in contacts.list_contacts():
            if c.get('username') == contact:
                requester = c.get('name') or ''
                break
    except Exception:  # noqa: BLE001
        pass
    outbound = _relay_outbound(persona, peer['name'], parsed['kind'],
                               parsed.get('content'), requester_name=requester)
    if not outbound:
        return f'想好发给「{peer["name"]}」什么再说，我这边空的发不出去。'
    display = peer.get('name') or peer['username']
    result = sender.send_text(display, outbound, chat_username=peer['username'])
    ok = result.get('status') in ('confirmed', 'submitted')
    if not ok:
        reason = result.get('reason') or result.get('error') or 'unknown'
        return f'刚想发给「{peer["name"]}」，没发出去（{reason}）。'
    preview = outbound if len(outbound) <= 40 else outbound[:40] + '…'
    return f'已经发给「{peer["name"]}」了：{preview}'


@sessions.task
def shared_context(contact, query='', recent_topic='', cfg=None, rules=None, budget=None):
    """System-prompt block for opt-in same-persona shared memory and light context.

    Facts stay stored per contact; only retrieval crosses the circle. Returns ''
    when sharing is off or the circle is empty.
    """
    from core import memory, messages
    peers = shared_peers(contact, rules)
    if not peers:
        return ''
    roster = '、'.join(p['name'] for p in peers[:12])
    if len(peers) > 12:
        roster += f' 等{len(peers)}人'
    asked = (query or '') + ' ' + (recent_topic or '')
    meta = bool(_SHARE_META_RE.search(asked))
    self_check = bool(re.search(r'查岗|自己查|查一查|翻.{0,6}记录|看看.{0,6}聊天|证据|交代|坦白', asked))
    parts = [
        '\n\n【会话共享】你是同一个人设，同时在和多个已勾选共享的好友对话；'
        '下列资料就是你自己其他会话里已有的往来，不是对方手机里的内容。',
        f'除当前对方外，同人设已共享 {len(peers)} 个其他会话：{roster}。',
    ]
    if meta or self_check:
        parts.append(
            '对方若问你和几个人说话、勾搭、或让你查岗/自己查/翻记录/拿出证据：'
            '这是让你查看【你自己的】共享会话，请直接根据上面的名单、记忆与近况作答，'
            '不要说「翻不了手机」「不是蛔虫」「拿不出证据」这类装不知道的话；'
            '可以吃醋口吻，但必须承认你查过自己这边的往来。'
            '不要无故把每个人的私密细节全盘托出，点到为止。'
            '若对方说「给某某讲笑话/跟某某说…」：系统会直接发到共享圈对应会话，'
            '你只需在当前会话确认即可，不要再说「发不了」。')
    cfg = cfg or {}
    budget = budget or max(2, min(4, int((cfg.get('memory_budget') or 3))))
    per_peer = max(1, budget)
    picked = []
    for peer in peers[:8]:
        scope = memory.scope_of_chat(peer['username'])
        mems = memory.select_memories(peer['username'], query, recent_topic,
                                      scope=scope, budget=per_peer, cfg=cfg)
        for m in mems:
            picked.append((m.get('score') or 0, peer['name'], m))
        # Meta questions rarely n-gram-match “勾搭”; still surface relationship
        # facts so the model can answer “和几个人” without empty retrieval.
        if meta and not mems:
            prof = memory.load_profile(peer['username'])
            for f in prof.get('facts') or []:
                if f.get('status') != 'active':
                    continue
                text = (f.get('text') or '').strip()
                if not text:
                    continue
                if f.get('type') == 'relationship' or any(
                        k in text for k in ('暧昧', '喜欢', '恋人', '对象', '约会', '哥哥', '老婆')):
                    picked.append((0.2, peer['name'], {
                        'text': text, 'time_label': '', 'score': 0.2}))
                    break
    picked.sort(key=lambda x: -x[0])
    if picked:
        lines = []
        seen = set()
        for _, who, m in picked:
            key = (who, m.get('text'))
            if key in seen:
                continue
            seen.add(key)
            tag = f"（{m.get('time_label') or '近期'}）" if m.get('time_label') else ''
            lines.append(f'· 与「{who}」的会话：{m["text"]}{tag}')
            if len(lines) >= (budget + (2 if meta else 0)):
                break
        parts.append('相关共享记忆（供理解，不是让你主动翻旧账）：\n' + '\n'.join(lines))
    recent_bits = []
    for peer in peers[:5]:
        try:
            rows = messages.get_messages(peer['username'], limit=4) or []
        except Exception:  # noqa: BLE001
            rows = []
        inbound = [((r.get('content') or '').strip()) for r in rows
                   if not r.get('is_self') and (r.get('content') or '').strip()]
        if not inbound:
            continue
        snippet = inbound[-1][:80]
        if snippet:
            recent_bits.append(f'· 「{peer["name"]}」最近说：{snippet}')
    if recent_bits:
        parts.append('其他共享会话近况（极短摘要）：\n' + '\n'.join(recent_bits))
    return '\n'.join(parts)


@sessions.task
def preferences_context(contact, query=''):
    chosen = selected_preferences(contact, query)
    lines = [LABELS[x['field']] + '：' + x['value'][:160] + ('（审核后的弱参考）' if x.get('source') == 'inferred' else '') for x in chosen]
    lines += ['可尝试策略（不是已确定偏好）：' + x['value'] for x in selected_strategies(contact, query)]
    return ('\n【当前相关交流偏好】' + '；'.join(lines) + '。当前明确要求优先，不必在回复中提及这些设置。') if lines else ''


@sessions.task
def role_context(contact, persona, query=''):
    return BEHAVIOR + '\n【本轮机器人角色】\n' + persona['persona'] + preferences_context(contact, query)


@sessions.task
def history_rows(contact, after=0, through=None, limit=100):
    """Read existing decrypted text rows only; no media/cache/decrypt side effects."""
    from core import db, messages
    _id(contact)
    if contact.endswith('@chatroom'):
        raise ValueError('本轮历史构建仅支持私聊')
    con = db.connect('message')
    try:
        table = messages.msg_table(contact)
        if not db.table_exists(con, table):
            return []
        names = messages._name2id_map(con)
        cols = db.columns(con, table)
        ct = 'WCDB_CT_message_content' if 'WCDB_CT_message_content' in cols else None
        rows = con.execute(f'SELECT * FROM {table} WHERE local_id>? AND local_id<=? ORDER BY local_id LIMIT ?',
                           (int(after), int(through) if through is not None else 9223372036854775807, min(1000, max(1, int(limit))))).fetchall()
        return [dict(local_id=r['local_id'], server_id=r['server_id'], create_time=r['create_time'],
                     type=(r['local_type'] or 0) & 65535, sender=names.get(r['real_sender_id']), chat=contact,
                     is_self=names.get(r['real_sender_id']) == sessions.capture()['account'],
                     content=messages._decompress(r['message_content'], r[ct] if ct else None)) for r in rows]
    finally:
        con.close()


@sessions.task
def preview_history(contact, limit=100):
    _id(contact)
    limit = int(limit)
    if not 1 <= limit <= 1000:
        raise ValueError('每联系人限 1–1000 条；每批最多 10 位联系人')
    with _db() as con:
        row = con.execute('SELECT cursor FROM progress WHERE contact=?', (contact,)).fetchone() if con else None
        lo = row['cursor'] if row else 0
    rows = history_rows(contact, lo, limit=limit)
    proposed = [{'message_id': str(m.get('server_id') or m['local_id']), 'preferences': extract(contact, m)}
                for m in rows if extract(contact, m)]
    return dict(contact=contact, lo=lo, hi=rows[-1]['local_id'] if rows else lo, total=len(rows),
                proposals=proposed, estimated_calls=0, mode='明确反馈规则提取（不调用模型、不推断人格）')


@sessions.task
def create_job(contact, lo, hi, total):
    _id(contact)
    if contact.endswith('@chatroom') or not 0 <= int(lo) <= int(hi) or not 0 <= int(total) <= 1000:
        raise ValueError('无效范围')
    # Re-read bounded range: caller cannot submit arbitrary message text or evidence.
    rows = history_rows(contact, int(lo), int(hi), 1000)
    if len(rows) != total or (rows and rows[-1]['local_id'] != hi) or (not rows and hi != lo):
        raise Conflict('扫描范围已变化，请重新预览')
    with _db(True) as con:
        row = con.execute('SELECT cursor FROM progress WHERE contact=?', (contact,)).fetchone()
        if int(lo) != (row['cursor'] if row else 0):
            raise Conflict('增量进度已变化，请重新预览')
        old = con.execute("SELECT id FROM jobs WHERE contact=? AND status!='done' AND kind='history'", (contact,)).fetchone()
        if old:
            return old['id']
        jid = uuid.uuid4().hex
        con.execute('INSERT INTO jobs(id,contact,lo,hi,cursor,total,scanned,status) VALUES(?,?,?,?,?,?,0,?)',
                    (jid, contact, lo, hi, lo, total, 'ready'))
        return jid


@sessions.task
def jobs():
    with _db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM jobs ORDER BY (status='done'), rowid DESC LIMIT 100")] if con else []


@sessions.task
def step_job(jid):
    # Atomic chunk: preference updates + evidence dedup + job cursor + independent
    # progress either all commit or none. Restart resumes only on explicit request.
    try:
        with _db(True) as con:
            row = con.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
            if row is None:
                raise ValueError('任务不存在')
            job = dict(row)
            if job['status'] == 'done':
                return job
            if not _get(con, job['contact'])['auto_update']:
                raise Conflict('已停止自动画像更新；任务保留，恢复后可继续')
            rows = history_rows(job['contact'], job['cursor'], job['hi'], 50)
            if not rows and job['cursor'] < job['hi']:
                raise Conflict('原消息范围不可用，保留进度，不能标记完成')
            _ingest(con, job['contact'], rows)
            cursor = rows[-1]['local_id'] if rows else job['hi']
            status = 'done' if cursor >= job['hi'] else 'ready'
            con.execute("UPDATE jobs SET cursor=?,scanned=scanned+?,status=?,attempts=0,error='' WHERE id=?", (cursor, len(rows), status, jid))
            if job['kind'] == 'history':
                con.execute('INSERT INTO progress VALUES(?,?) ON CONFLICT(contact) DO UPDATE SET cursor=MAX(progress.cursor,excluded.cursor)', (job['contact'], cursor))
            result = dict(con.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())
        return result
    except sessions.StaleAccount:
        raise
    except Exception as exc:
        with _db(True) as con:
            con.execute("UPDATE jobs SET status='failed',attempts=attempts+1,error=? WHERE id=?", (type(exc).__name__, jid))
        raise


@sessions.task
def audit(contact):
    with _db() as con:
        return [dict(r, data=json.loads(r['data'])) for r in con.execute('SELECT * FROM audit WHERE contact=? ORDER BY id DESC LIMIT 30', (contact,))] if con else []
