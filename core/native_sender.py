"""Private-chat adapter using the client's native contact identity field.

Identity chain: contact card -> native Copy of WeChat ID -> unique contact DB
mapping -> that card's Send Message navigation -> unchanged chat header and live
client/account fingerprint. OCR locates controls only; it never supplies identity.
This is UI automation with a human-input race boundary, not exactly-once delivery.
Groups and unsupported payloads are explicitly rejected before model generation.
"""
import difflib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
import config
from core import account_session as sessions, db, decrypt, messages, send_ledger
from core.native_ui import NativeUI


class NativeContactAdapter:
    def __init__(self, ui=None):
        self.ui=ui or NativeUI()
        self.proof=None
        self.fingerprint=None
        self.last_reason=''

    @property
    def available(self):
        return self.ui.available

    def capability(self, chat, kind='text'):
        if kind!='text':return 'native_payload_not_supported'
        if not chat or chat.endswith('@chatroom'):return 'native_group_identity_unavailable'
        try:
            self._contact(chat)
        except (ValueError, OSError, sqlite3.Error):return 'native_contact_identity_unavailable'
        try:
            self._state()
        except Exception:
            return 'native_client_state_unavailable'
        return ''

    def _contact(self, chat):
        c=db.connect('contact')
        try:
            row=c.execute('SELECT username,alias,remark,nick_name FROM contact WHERE username=?',(chat,)).fetchone()
            if not row:raise ValueError('contact_missing')
            row=dict(row);identity=(row['alias'] or '').strip() or row['username']
            found=c.execute('SELECT username FROM contact WHERE alias=? OR username=?',(identity,identity)).fetchall()
            if len(found)!=1 or found[0]['username']!=chat:raise ValueError('identity_not_unique')
            row['identity']=identity
            return row
        finally:c.close()

    def _state(self):
        state=self.ui.call('state');token=sessions.check()
        actual=config._strip_folder_suffix(state['account_folder'])
        if actual!=token['account']:raise RuntimeError('client_account_mismatch')
        # X11 geometry currently validated only against this Linux client layout.
        g=state['geometry']
        if g['WIDTH']!=1022 or g['HEIGHT']!=741:raise RuntimeError('client_layout_not_supported')
        return state

    def _point(self,row):
        self.ui.call('click',x=row['point'][0],y=row['point'][1])

    def _card(self,state):
        g=state['geometry'];x,y=g['X'],g['Y']
        rows=self.ui.ocr((x+286,y+70,x+1022,y+741))
        ids=[r for r in rows if re.match(r'^微信号\s*[:：]',r['text'])]
        buttons=[r for r in rows if r['text']=='发消息']
        if len(ids)!=1 or len(buttons)!=1:return None
        return ids[0],buttons[0]

    def _copy_identity(self, field):
        for attempt in range(3):
            try:
                return self._copy_identity_once(field)
            except RuntimeError as exc:
                if str(exc) not in ('clipboard_identity_unchanged','invalid_copied_identity','native_copy_unavailable') or attempt==2:
                    raise
                time.sleep(.3)

    def _copy_identity_once(self, field):
        sentinel='WX_ID_READ_'+uuid.uuid4().hex
        field_point=field.get('right_point',field['point'])
        original=self.ui.call('copy_field',x=field_point[0],y=field_point[1],sentinel=sentinel)['original']
        try:
            px,py=field_point
            menu=self.ui.ocr((int(px),int(py),int(px)+150,int(py)+90))
            copy=[r for r in menu if r['text']=='复制']
            if len(copy)!=1:
                self.ui.call('key',keys=['Escape']);raise RuntimeError('native_copy_unavailable')
            self._point(copy[0])
            value=''
            for _ in range(8):
                value=self.ui.call('clipboard')['value'].strip()
                if value and value!=sentinel:break
                time.sleep(.15)  # Native Qt clipboard ownership/data settle asynchronously.
            value=re.sub(r'^微信号\s*[:：]\s*','',value)
            if value==sentinel:raise RuntimeError('clipboard_identity_unchanged')
            if not re.fullmatch(r'[A-Za-z0-9_.-]{3,100}',value):raise RuntimeError('copied_identity_includes_label' if value.startswith('微信号') else 'invalid_copied_identity')
            return value
        finally:self.ui.call('restore',value=original)

    def _verify_card(self,state,contact):
        card=self._card(state)
        if not card:return False
        field,button=card
        # OCR is a negative/navigation filter only; acceptance still requires
        # two exact copies. A recognition miss may block, never authorize a send.
        hint=re.sub(r'^微信号\s*[:：]\s*','',field.get('text',''))
        if hint and difflib.SequenceMatcher(None,hint,contact['identity']).ratio()<.6:
            return False
        try:
            actual=self._copy_identity(field)
        except RuntimeError as exc:
            if str(exc) in ('clipboard_identity_unchanged','invalid_copied_identity','native_copy_unavailable'):
                return False
            raise
        if actual!=contact['identity']:return False
        # Repeat the native read, then locate navigation again. This also rejects
        # UI changes that occurred while the clipboard/menu was being read.
        card=self._card(state)
        if not card or self._copy_identity(card[0])!=actual:return False
        again=self._state()
        if again!=state:raise RuntimeError('client_changed_during_identity_check')
        card=self._card(state)
        if not card:return False
        self._point(card[1])  # Opens this verified contact's chat; does not send.
        time.sleep(.65)
        fp=self.ui.call('fingerprint')
        if fp['signature']!=state['signature'] or not fp['active']:raise RuntimeError('client_changed_during_navigation')
        self.fingerprint=fp
        self.proof=dict(trusted=True,account=sessions.capture()['account'],generation=sessions.capture()['generation'],
                        chat=contact['username'],session_identity=state['signature'],identity_method='native_contact_wechat_id_copy')
        return True

    def open(self,display,chat):
        self.proof=None;self.fingerprint=None;self.last_reason=''
        try:
            contact=self._contact(chat);state=self._state();g=state['geometry'];x,y=g['X'],g['Y']
            # Use Contacts, never an Enter key on an ambiguous search result.
            self.ui.call('key',keys=['Escape'])
            self.ui.call('click',x=x+32,y=y+151)
            time.sleep(.2)
            if self._verify_card(state,contact):return True
            candidates={(contact.get('remark') or '').strip(),(contact.get('nick_name') or '').strip(),contact['identity']}-{''}
            # Names are navigation hints only. Every candidate must pass native ID
            # verification, so same-name contacts cannot pass based on their name.
            for _ in range(18):self.ui.call('scroll',x=x+220,y=y+350,up=True)
            previous=None
            for page in range(12):
                rows=self.ui.ocr((x+65,y+70,x+285,y+741))
                layout=tuple((r['text'],tuple(round(v) for v in r['point'])) for r in rows)
                if layout==previous:break
                previous=layout
                # OCR may omit emoji or punctuation in a nickname. Inspect every
                # visible contact row when the name hint cannot match; acceptance
                # still depends solely on the copied native ID, never fuzzy names.
                def rank(row):
                    norm=lambda value: ''.join(c for c in value if c.isalnum())
                    return max((difflib.SequenceMatcher(None,norm(row['text']),norm(name)).ratio() for name in candidates),default=0)
                rows=sorted(rows,key=rank,reverse=True)
                for row in rows:
                    if row['text'] in {'联系人','新的朋友','群聊','公众号','通讯录管理','标签'}:
                        continue
                    if row['point'][1] < y+195:
                        continue
                    self._point(row)
                    if self._verify_card(state,contact):return True
                for _ in range(5):self.ui.call('scroll',x=x+220,y=y+550,up=False)
            self.last_reason='native_contact_not_found'
        except Exception as exc:
            self.last_reason=str(exc) if isinstance(exc,RuntimeError) and re.fullmatch('[a-z_]+',str(exc)) else 'native_identity_check_failed'
        return False

    def identity(self):
        if not self.proof:return None
        sessions.refresh_identity()
        state=self._state();fp=self.ui.call('fingerprint')
        if (not fp['active'] or fp['header']!=self.fingerprint['header'] or
            fp['signature']!=self.fingerprint['signature'] or fp['geometry']!=self.fingerprint['geometry']):return None
        if sessions.capture()['generation']!=self.proof['generation']:return None
        return dict(self.proof)

    def _message_snapshot(self,chat):
        # Existing keys, fresh isolated snapshot. Never modify the poller's DB.
        with tempfile.TemporaryDirectory(prefix='wx-send-receipt-') as td:
            path=str(Path(td)/'message.db')
            keys=decrypt.load_keys();rel=config.CORE_DBS['message']
            decrypt.decrypt_db(os.path.join(config.db_storage_dir(),rel),keys[rel],path)
            con=sqlite3.connect('file:'+path+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
            try:
                table=messages.msg_table(chat)
                if not db.table_exists(con,table):return []
                names=messages._name2id_map(con)
                out=[]
                for r in con.execute(f'SELECT * FROM {table} ORDER BY local_id DESC LIMIT 60'):
                    m=dict(r)
                    out.append(dict(local_id=m['local_id'],server_id=m.get('server_id'),
                                    sender=names.get(m.get('real_sender_id')),type=(m.get('local_type') or 0)&65535,
                                    time=m.get('create_time') or 0,
                                    digest=send_ledger.stable_id(messages._decompress(m.get('message_content'),m.get('WCDB_CT_message_content')))))
                return out
            finally:con.close()

    def prepare_send(self,kind,payload,jid):
        if kind!='text' or not isinstance(payload,str) or not payload.strip():raise RuntimeError('native_payload_not_supported')
        if not self.identity():raise RuntimeError('target_changed')
        baseline=max((r['local_id'] for r in self._message_snapshot(self.proof['chat'])),default=0)
        data=dict(proof=self.proof,baseline=baseline,digest=send_ledger.stable_id(payload),created=time.time())
        ledger=send_ledger.Ledger()
        with ledger.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS native_receipts (job TEXT PRIMARY KEY, data TEXT NOT NULL)')
            c.execute('INSERT OR IGNORE INTO native_receipts VALUES(?,?)',(jid,json.dumps(data)))
        self.ui.call('stage',signature=self.fingerprint['signature'],header=self.fingerprint['header'],
                     text=payload,sentinel='WX_EMPTY_'+uuid.uuid4().hex)
        if not self.identity():raise RuntimeError('target_changed_after_staging')

    def cancel_staging(self, payload):
        if self.fingerprint and isinstance(payload,str):
            self.ui.call('discard_stage',signature=self.fingerprint['signature'],header=self.fingerprint['header'],
                         text=payload,sentinel='WX_CLEAR_'+uuid.uuid4().hex)

    def send(self,kind,payload,jid):
        # Coordinator commits initiated before this single click. No retry here.
        self.ui.call('send_once',signature=self.fingerprint['signature'],header=self.fingerprint['header'])

    def receipt(self,jid):
        ledger=send_ledger.Ledger()
        with ledger.connect() as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='native_receipts'").fetchone():return None
            row=c.execute('SELECT data FROM native_receipts WHERE job=?',(jid,)).fetchone()
        if not row:return None
        data=json.loads(row['data']);proof=data['proof']
        if not sessions.valid({k:proof[k] for k in ('account','generation')}):return None
        # Matching a newly assigned native server ID confirms an observed target
        # row, not exclusive delivery or proof that nobody manually sent same text.
        for _ in range(8):
            try:
                state=self._state()
                if state['signature']!=proof['session_identity']:return None
                rows=self._message_snapshot(proof['chat'])
                matches=[r for r in rows if r['local_id']>data['baseline'] and r['sender']==proof['account'] and
                         r['server_id'] and r['type']==1 and r['digest']==data['digest'] and r['time']>=data['created']-2]
                if len(matches)==1:return dict(proof,job_id=jid,message_id=str(matches[0]['server_id']))
                if len(matches)>1:return None
            except (OSError,sqlite3.Error,RuntimeError):pass
            time.sleep(.5)
        return None
