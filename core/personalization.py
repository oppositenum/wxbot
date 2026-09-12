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
BEHAVIOR = ('【通用要求】真实、切题，不能编造共同经历或角色经历作为现实事实。'
            '当前明确请求决定本轮任务和长度，优先于交流偏好。角色决定机器人是谁，偏好仅辅助表达。'
            '旧机器人回复只供理解对话，不是当前角色指令。事实记忆不因角色切换失效。'
            '不主动翻旧事、不报告画像标签、不强制追问、安慰或昵称。对方结束交流时尊重其意愿。')
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
                conversation_control_enabled=False, preferences={}, revision=0)


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
    for key in ('persona_id', 'personalization_enabled', 'auto_update', 'conversation_control_enabled'):
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
    if set(patch) - {'persona_id', 'personalization_enabled', 'auto_update', 'conversation_control_enabled', 'preferences'}:
        raise ValueError('未知设置字段')
    with _db(True) as con:
        data = _get(con, contact)
        if data['revision'] != revision:
            raise Conflict('配置已被修改，请刷新后重试')
        data.pop('config_error', None)
        for key in ('persona_id', 'personalization_enabled', 'auto_update', 'conversation_control_enabled'):
            if key in patch:
                v = patch[key]
                if key == 'persona_id':
                    _slug(v)
                elif not isinstance(v, bool):
                    raise ValueError('开关必须是布尔值')
                data[key] = v
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


@sessions.task
def set_global(slug, revision, rules):
    """Explicit, auditable migration. Conflicts require manual rule reconciliation."""
    _slug(slug)
    inv = legacy_inventory(rules)
    current = get('__global__')
    if current.get('config_error'):
        raise Conflict('全局存储损坏，需要恢复备份，不能自动迁移')
    if not current['revision'] and inv['conflict']:
        raise Conflict('旧规则人设冲突，请先逐项核对；不能自动迁移')
    if not current['revision'] and inv['unique'] and slug != inv['unique']:
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
