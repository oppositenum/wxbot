"""Small durable outbox. Payloads stay local for bounded pre-send recovery; never log them.
SQLite commits intent BEFORE any possibly sending UI action. No replay worker.
"""
import contextlib
import contextvars
import hashlib
import json
import os
import sqlite3
import time
import uuid

import config
from core import account_session as sessions

_operation = contextvars.ContextVar('send_operation', default=None)


def stable_id(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


@contextlib.contextmanager
def operation(key):
    mark = _operation.set([str(key), 0])
    try:
        yield _operation.get()
    finally:
        _operation.reset(mark)


def next_id():
    op = _operation.get()
    if op is None:
        return uuid.uuid4().hex
    op[1] += 1
    return stable_id(op[0], op[1])


class Ledger:
    def __init__(self, path=None):
        self.path = path or os.path.join(config.WORK_DIR, 'send-ledger.sqlite3')
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS sends (
                id TEXT PRIMARY KEY, account TEXT NOT NULL, generation TEXT NOT NULL,
                chat TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT NOT NULL,
                status TEXT NOT NULL, reason TEXT NOT NULL, attempts INTEGER NOT NULL,
                retry_at REAL NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                payload TEXT)''')
            c.execute('CREATE TABLE IF NOT EXISTS proactive_guards (job TEXT PRIMARY KEY, data TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS held_replies (id TEXT PRIMARY KEY, account TEXT NOT NULL, '
                      'generation TEXT NOT NULL, chat TEXT NOT NULL, held_at REAL NOT NULL, data TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS reply_decisions (job TEXT PRIMARY KEY, account TEXT NOT NULL, '
                      'chat TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, '
                      'created REAL NOT NULL)')
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA synchronous=FULL')
        try:
            with c:
                yield c
        finally:
            c.close()

    def bind_proactive(self, jid, ticket):
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO proactive_guards VALUES(?,?)', (jid, json.dumps(ticket, sort_keys=True)))
            stored = c.execute('SELECT data FROM proactive_guards WHERE job=?', (jid,)).fetchone()
            if json.loads(stored['data']) != ticket:
                raise ValueError('proactive_job_context_conflict')

    def audit(self, jid, token, chat, status, reason, elapsed_ms):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO reply_decisions VALUES(?,?,?,?,?,?,?)',
                      (jid, token['account'], chat, status, reason, elapsed_ms, time.time()))
            c.execute('DELETE FROM reply_decisions WHERE job NOT IN '
                      '(SELECT job FROM reply_decisions ORDER BY created DESC LIMIT 500)')

    def decisions(self, account):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM reply_decisions WHERE account=? '
                    'ORDER BY created DESC LIMIT 30', (account,))]

    def proactive(self, jid):
        with self.connect() as c:
            row = c.execute('SELECT data FROM proactive_guards WHERE job=?', (jid,)).fetchone()
            return json.loads(row['data']) if row else None

    def get(self, jid):
        with self.connect() as c:
            r = c.execute('SELECT * FROM sends WHERE id=?', (jid,)).fetchone()
            return dict(r) if r else None

    def hold_reply(self, chat, pending):
        token = sessions.check()
        owner = pending.get('session') or token
        if owner['account'] != token['account']:
            raise sessions.StaleAccount('held_reply_account_mismatch')
        jid = pending.get('job_id') or stable_id(owner, chat, 'held', pending.get('msgs'))
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO held_replies VALUES(?,?,?,?,?,?)',
                      (jid, owner['account'], owner['generation'], chat, time.time(),
                       json.dumps(pending, ensure_ascii=False)))
        return jid

    def prepare(self, jid, token, chat, kind, digest='', payload=None):
        now = time.time()
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO sends VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (jid, token['account'], token['generation'], chat or '', kind,
                       digest, 'prepared', 'intent_committed', 0, 0, now, now,
                       json.dumps(payload, ensure_ascii=False) if payload is not None else None))
        return self.get(jid)

    def update(self, jid, status, reason, retry_at=0):
        with self.connect() as c:
            c.execute('UPDATE sends SET status=?,reason=?,retry_at=?,updated=? WHERE id=?',
                      (status, reason, retry_at, time.time(), jid))
        return self.get(jid)

    def begin_work(self, jid):
        with self.connect() as c:
            cur = c.execute("UPDATE sends SET status='working',updated=? WHERE id=? AND status='prepared'",
                            (time.time(), jid))
            return cur.rowcount == 1

    def claim(self, jid):
        """CAS excludes concurrent workers/processes before they touch the UI."""
        with self.connect() as c:
            cur = c.execute("UPDATE sends SET status='checking',attempts=attempts+1,updated=? "
                            "WHERE id=? AND status IN ('prepared','not_sent','deferred') AND attempts<3 "
                            "AND retry_at<=?", (time.time(), jid, time.time()))
            return cur.rowcount == 1

    def view(self, row):
        status, reason = row['status'], row['reason']
        if status == 'initiated':
            status, reason = 'uncertain', 'interrupted_after_intent_to_send'
        elif status == 'working':
            status, reason = 'uncertain', 'interrupted_model_or_tool_round'
        elif status in ('prepared', 'checking'):
            current = sessions.observe()
            if row['generation'] != current['generation'] or row['account'] != current['account']:
                status, reason = 'stale', 'prior_session_before_send_requires_review'
        return {'ok': status in ('confirmed', 'submitted'), 'status': status, 'reason': reason,
                'error': '' if status in ('confirmed', 'submitted') else reason,
                'job_id': row['id'], 'attempts': row['attempts'],
                'retry_at': row['retry_at'],
                'retryable': ((status == 'not_sent' and row['reason'] == 'open_failed') or
                              (status == 'deferred' and row['reason'] == 'reply_snapshot_unavailable'))
                             and row['attempts'] < 3,
                'verified': status == 'confirmed'}


def blocked_by_uncertainty(kind, chat):
    """A model must not retry an uncertain tool send as a new call in this round."""
    op = _operation.get()
    if not op:
        return False
    ledger = Ledger()
    for index in range(1, op[1] + 1):
        row = ledger.get(stable_id(op[0], index))
        if row and row['kind'] == kind and row['chat'] == chat and ledger.view(row)['status'] == 'uncertain':
            return True
    return False


def result(jid):
    ledger = Ledger()
    row = ledger.get(jid)
    return ledger.view(row) if row else None
