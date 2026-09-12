"""Small reply decisions and pre-dispatch checks shared by automatic outputs.

Private-chat rules are deliberately conservative: questions and explicit repeat
requests must not be dropped by lexical similarity. No model calls in guards.
"""
import contextlib
import contextvars
import re
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from core import account_session as sessions, reply_context, send_ledger

_current = contextvars.ContextVar('automatic_reply', default=None)
_CLOSINGS = {
    '好', '好的', '好嘞', '好哒', '好滴', '好吧', '好的呢', '好的呀', '好的哈', '好的哦',
    '明白', '明白了', '知道了', '知道啦', '了解', '了解了', '懂了',
    '收到', '收到啦', '嗯', '嗯嗯', '嗯好', '嗯好的', '嗯嗯好的',
    '行', '行的', '行吧', '没问题', 'ok', 'okay', '谢谢', '谢谢你', '谢了',
    '不客气', '拜拜', '再见', '晚安', '先这样', '先这样吧', '就这样', '就这样吧',
}
_CANCEL = {'不用回了', '不用回复了', '不用回答了', '别回复了', '先别回了', '先不用回了', '先不要发了', '不用继续了'}
_REQUEST = re.compile(r'[?？]|怎么|为什么|什么|哪里|哪个|多少|几点|何时|是否|能否|可否|吗|么|'
                      r'再说|再发|重复|重说|解释|说明|请|帮我|告诉我|发我|给我|查一下|翻译|总结')
LABELS = {
    'single_closing_message': '单条确认或结束语，无需回复',
    'self_message': '本账号发言，不触发自动回复',
    'reply_cancelled_by_user': '对方明确要求停止回复',
    'own_reply_after_source': '本账号已接着回复，跳过旧问题',
    'own_reply_before_send': '生成期间本账号已回复，取消旧回复',
    'newer_inbound_before_send': '对方有新消息，等待合并后重新处理',
    'reply_snapshot_unavailable': '消息快照暂不可核对，保留待处理记录',
    'recent_reply_duplicate': '与近期发言重复，跳过本次回复',
    'bot_stopped_before_send': '机器人已停止，取消待发回复',
}


def label(reason):
    return LABELS.get(reason, reason)


def scoped():
    return _current.get() is not None


def timing(stage, started):
    ticket = _current.get()
    if ticket and ticket.get('log'):
        ticket['log'](f'[回复耗时] {stage} {round((time.monotonic()-started)*1000)}ms')


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str


def plain(text):
    return unicodedata.normalize('NFKC', str(text or '')).lower().strip()


def standalone(msg):
    return msg.get('type') == 1 and not any(msg.get(k) for k in
        ('refer', 'at_me', 'at_all', 'quote_me'))


def closing(batch):
    if len(batch) != 1 or not standalone(batch[0]) or batch[0].get('is_self'):
        return False
    text = plain(batch[0].get('content'))
    if '?' in text:
        return False
    text = text.rstrip(' \t\r\n.,!~;，。！～；…👍👌🙏😊🙂\ufe0f')
    parts = [p for p in re.split(r'[,，。.!！;；\s]+', text) if p]
    return bool(parts) and all(p in _CLOSINGS for p in parts)


def decide(batch, context, account):
    incoming = [m for m in batch if not reply_context.is_self(m, account)]
    if not incoming:
        return Decision('observe', 'self_message')
    last = incoming[-1]
    if standalone(last) and plain(last.get('content')).rstrip('。.!！ ') in _CANCEL:
        return Decision('observe', 'reply_cancelled_by_user')
    if closing(incoming):
        return Decision('observe', 'single_closing_message')
    source_id = max((m.get('local_id') or 0 for m in incoming), default=0)
    if source_id and any(reply_context.is_self(m, account) and
                         (m.get('local_id') or 0) > source_id and meaningful(m)
                         for m in context):
        return Decision('observe', 'own_reply_after_source')
    return Decision('reply', 'incoming_request' if any(_REQUEST.search(m.get('content') or '')
                                                     for m in incoming) else 'conversation_continues')


def meaningful(msg):
    return msg.get('type') in (1, 3, 34, 43, 47, 49) and not msg.get('revoke') and not (
        (msg.get('content') or '').startswith('【定时提醒】'))


@contextlib.contextmanager
def scope(chat, batch, context=(), *, mode='reply', log=None):
    token = sessions.capture()
    rows = list(context) + list(batch)
    ticket = dict(chat=chat, session=token, batch=list(batch), context=list(context), mode=mode,
                  after=max((m.get('local_id') or 0 for m in rows), default=0),
                  started=time.monotonic(), sent=False, log=log)
    marker = _current.set(ticket)
    try:
        yield ticket
    finally:
        _current.reset(marker)


def normalized(text):
    return ''.join(c for c in plain(text) if c.isalnum())


def near(left, right):
    a, b = normalized(left), normalized(right)
    if not a or not b:
        return False
    if a == b:
        return True
    # Changed dates, quantities, URLs or negation must not be treated as filler.
    if min(len(a), len(b)) < 16 or re.findall(r'\d+', a) != re.findall(r'\d+', b):
        return False
    if re.findall(r'不|没|无|别|未', a) != re.findall(r'不|没|无|别|未', b):
        return False
    if re.search(r'https?://', left + right):
        return False
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= .94


def _recent(ledger, token, chat, jid):
    # Existing durable send records cover replies, greetings and follow-ups.
    with ledger.connect() as con:
        rows = con.execute("SELECT payload FROM sends WHERE account=? AND chat=? AND kind='text' "
            "AND status IN ('initiated','submitted','confirmed','uncertain') AND id<>? "
            "AND created>=? ORDER BY created DESC LIMIT 40",
            (token['account'], chat, jid, time.time()-1800)).fetchall()
    import json
    return [json.loads(row['payload']) for row in rows if row['payload']]


def before_dispatch(chat, kind, payload, ledger, jid):
    """Called under the single UI lock, immediately before possible UI sending."""
    ticket = _current.get()
    if ticket is None:
        return None
    from core import conversation_state, reply_inbox
    sessions.check(ticket['session'])
    if not reply_inbox.enabled():
        return Decision('observe', 'bot_stopped_before_send')
    try:
        reply_inbox.refresh()
        newer = conversation_state.latest(ticket['chat'], after=ticket['after'] + 1)
    except sessions.StaleAccount:
        raise
    except Exception:
        return Decision('defer', 'reply_snapshot_unavailable')
    relevant = [m for m in newer if meaningful(m)]
    account = ticket['session']['account']
    incoming = [m for m in relevant if not reply_context.is_self(m, account)]
    if ticket['chat'].endswith('@chatroom'):
        sources = {m.get('sender') for m in ticket['batch'] if m.get('sender')}
        incoming = [m for m in incoming if m.get('sender') in sources or m.get('at_me') or m.get('quote_me')]
    # Rebuild on new input, even when it contains a question after an own reply.
    if incoming and standalone(incoming[-1]) and plain(incoming[-1].get('content')).rstrip('。.!！ ') in _CANCEL:
        return Decision('observe', 'reply_cancelled_by_user')
    # Ordinary inbound belongs to the next fixed batch. Greetings are still
    # cancelled when their proactive context changes.
    if incoming and (ticket['mode'] != 'reply' or chat != ticket['chat']):
        return Decision('defer', 'newer_inbound_before_send')
    prior = _recent(ledger, ticket['session'], ticket['chat'], jid)
    own = [m for m in relevant if reply_context.is_self(m, account)]
    if any(m.get('type') != 1 or not any(normalized(m.get('content')) == normalized(t) for t in prior)
           for m in own):
        return Decision('observe', 'own_reply_before_send')
    if kind == 'text' and isinstance(payload, str):
        requested = ticket['mode'] == 'reply' and any(
            _REQUEST.search(m.get('content') or '') or m.get('refer') or m.get('type') != 1
            for m in ticket['batch'])
        if not requested:
            texts = _recent(ledger, ticket['session'], chat, jid)
            if chat == ticket['chat']:
                texts += [m.get('content') or '' for m in ticket['context']
                          if reply_context.is_self(m, account) and m.get('type') == 1
                          and (m.get('create_time') or 0) >= time.time()-1800][-6:]
            if any(near(payload, text) for text in texts if isinstance(text, str)):
                return Decision('observe', 'recent_reply_duplicate')
    ticket['sent'] = True
    return None


def record(decision, ledger, jid):
    ticket = _current.get()
    status = 'deferred' if decision.action == 'defer' else 'skipped'
    if ticket and ticket.get('log'):
        ticket['log']('[回复决策] ' + label(decision.reason))
    retry_at = time.time() + 5 if decision.reason == 'reply_snapshot_unavailable' else 0
    return ledger.view(ledger.update(jid, status, decision.reason, retry_at))
