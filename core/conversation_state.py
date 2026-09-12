"""Temporary conversation closure and durable no-proactive consent. No models.

Uses communication.sqlite3, separate from profiles/settings and scheduled tasks.
Only server-observed incoming messages advance state. Reading latest decrypted
rows cannot detect a message that WeChat has not yet exposed to that database.
"""
import contextlib
import json
import re
import sqlite3
import time
from core import personalization as p, account_session as sessions


def classify(text):
    text = re.sub(r'[\s，,。.!！~～]+', '', text or '')
    if not text or re.search(r'[“”"「」？?]|他说|她说|你说|转述|假如|如果|是不是|不是|不想说拜拜|别说拜拜', text):
        return None
    if re.fullmatch(r'(以后|今后)(请)?(不要|别)(再)?主动(给我)?(发消息|联系我|找我|跟我聊天)', text):
        return 'no_proactive'
    if re.fullmatch(r'(以后|今后)(你)?(可以|允许你)主动(给我发消息|联系我|找我)', text):
        return 'allow_proactive'
    if re.fullmatch(r'(这个|这件事|这个话题)(先)?(不聊了|别聊了|不想聊了)|先换个话题', text):
        return 'topic_only'
    if re.fullmatch(r'(先这样|就这样)(吧)?(拜拜|再见|晚安)?|拜拜(吧)?|再见|晚安|不想(跟你)?聊了|不想跟你说话了(拜拜吧)?|先不聊了|今天不聊了|我先去忙了(拜拜)?', text):
        return 'closed'
    return None


def genuine(chat, m):
    # Groups intentionally do not inherit an individual member's private state.
    return (not chat.endswith('@chatroom') and m.get('is_self') is False
            and m.get('sender') == chat and m.get('chat', chat) == chat
            and m.get('type') in (1, 3, 34, 43, 47, 49)
            and not any(m.get(k) for k in ('system', 'scheduled', 'revoke', 'fictional'))
            and m.get('origin', 'user') == 'user' and bool(m.get('local_id')))


def _get(con, chat):
    row = con.execute('SELECT data FROM conversation_state WHERE chat=?', (chat,)).fetchone() if con else None
    return json.loads(row['data']) if row else dict(revision=0, last_id=0, last_fingerprint='', paused=False,
                                                   no_proactive=False, reason='', evidence_id=None)


@sessions.task
def get(chat):
    p._id(chat)
    with p._db() as con:
        if con and not con.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_state'").fetchone():
            return _get(None, chat)
        return _get(con, chat)


@sessions.task
def observe(chat, rows):
    if not p.get(chat)['conversation_control_enabled']:
        return get(chat)
    incoming = sorted([m for m in rows if genuine(chat, m)], key=lambda m: m['local_id'])
    if not incoming:
        return get(chat)
    with p._db(True) as con:
        state = _get(con, chat)
        for m in incoming:
            # Reinspect the latest message if native voice text appears later.
            native = m.get('voice_transcript') if m.get('voice_transcript_source') == 'wechat_packed_v1' else ''
            text = native if m['type'] == 34 else m.get('content', '') if m['type'] == 1 else ''
            fingerprint = p.hashlib.sha256(text.encode()).hexdigest()
            if m['local_id'] < state['last_id'] or (m['local_id'] == state['last_id'] and fingerprint == state['last_fingerprint']):
                continue
            new_message = m['local_id'] > state['last_id']
            event = classify(text) if not m.get('refer') else None
            if new_message:
                state['paused'] = False
                state['reason'] = 'new_inbound'
            if event == 'closed':
                state['paused'] = True
            elif event == 'no_proactive':
                state['no_proactive'] = True
            elif event == 'allow_proactive':
                state['no_proactive'] = False
            if event:
                state['reason'] = event
            state.update(last_id=m['local_id'], last_fingerprint=fingerprint, revision=state['revision']+1,
                         evidence_id=str(m.get('server_id') or m['local_id']), updated=int(time.time()))
        con.execute('INSERT OR REPLACE INTO conversation_state VALUES(?,?)', (chat, json.dumps(state)))
        return state


@sessions.task
def latest(chat, after=0):
    """Read-only recent native messages, no media fetch/cache/decryption/model."""
    from core import db, messages, voice_text
    con = db.connect('message')
    try:
        table = messages.msg_table(chat)
        if not db.table_exists(con, table):
            return []
        names = messages._name2id_map(con)
        cols = db.columns(con, table)
        # Bounded catch-up; if larger, fail closed until a normal poll advances it.
        rows = con.execute(f'SELECT * FROM {table} WHERE local_id>=? ORDER BY local_id LIMIT 1001', (after,)).fetchall()
        if len(rows) > 1000:
            raise p.Conflict('主动发送前入站积压未完成核对')
        out = []
        for row in rows:
            m = dict(row);typ = (m.get('local_type') or 0) & 65535
            source = names.get(m.get('real_sender_id'))
            content = messages._decompress(m.get('message_content'), m.get('WCDB_CT_message_content'))
            if chat.endswith('@chatroom'):
                group_sender, content = messages._split_group_sender(content)
                source = group_sender or source
            account = sessions.capture()['account']
            source_xml = messages._decompress(m.get('source'), m.get('WCDB_CT_source'))
            at_list = messages._parse_atlist(source_xml)
            item = dict(local_id=m['local_id'],server_id=m.get('server_id'),sender=source,
                        chat=chat,is_self=source==account,type=typ,content=content,
                        create_time=m.get('create_time'), at_me=account in at_list,
                        at_all=any(a.endswith('@all') for a in at_list))
            if typ == 49:
                title, ref = messages._parse_refer(content)
                if ref:
                    item.update(content=title or content, refer=ref, quote_me=ref.get('chatusr') == account)
            if typ == 34:
                item.update(voice_transcript=voice_text.from_packed(m.get('packed_info_data')),voice_transcript_source='wechat_packed_v1')
            out.append(item)
        return out
    finally:
        con.close()


@sessions.task
def ticket(chat, rows=None):
    if chat.endswith('@chatroom'):
        return dict(session=sessions.capture(), chat=chat, revision=0, last_id=0)
    if not p.get(chat)['conversation_control_enabled']:
        return dict(session=sessions.capture(), chat=chat, revision=0, last_id=0, control_enabled=False)
    try:
        state = observe(chat, rows if rows is not None else latest(chat, get(chat)['last_id']))
    except (OSError, sqlite3.Error, p.Conflict):
        return None
    if state['paused'] or state['no_proactive']:
        return None
    return dict(session=sessions.capture(), chat=chat, revision=state['revision'], last_id=state['last_id'], control_enabled=True)


@sessions.task
def allowed(ticket_, refresh=True):
    if not ticket_ or ticket_.get('session') != sessions.capture():
        return False
    chat = ticket_['chat']
    if chat.endswith('@chatroom'):
        return True
    enabled = p.get(chat)['conversation_control_enabled']
    if enabled != ticket_.get('control_enabled', True):
        return False  # Configuration changed during generation; discard old result.
    if not enabled:
        return True
    if refresh:
        try:
            state = observe(chat, latest(chat, get(chat)['last_id']))
        except (OSError, sqlite3.Error, p.Conflict):
            return False
    else:
        state = get(chat)
    return not (state['paused'] or state['no_proactive']) and state['revision'] == ticket_['revision']


@contextlib.contextmanager
def final_guard(ticket_):
    """Coordinator holds UI lock first. Refresh, then serialize state check->send.

    Lock order: UI -> SQLite communication write -> account epoch. State observers
    never take UI lock. No model call is made while holding this transaction.
    """
    if ticket_ and ticket_['chat'].endswith('@chatroom'):
        yield ticket_['session'] == sessions.capture()
        return
    if not allowed(ticket_):
        yield False
        return
    with p._db(True) as con:
        enabled = p._get(con, ticket_['chat'])['conversation_control_enabled']
        if enabled != ticket_.get('control_enabled', True):
            yield False
            return
        if not enabled:
            yield ticket_['session'] == sessions.capture()
            return
        state = _get(con, ticket_['chat'])
        ok = (ticket_['session'] == sessions.capture() and state['revision'] == ticket_['revision']
              and not state['paused'] and not state['no_proactive'])
        yield ok
