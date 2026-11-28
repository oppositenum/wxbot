"""bot 回复的「直发」通道:与 LegacySearchAdapter 同一套账本/复核约定,最后一步改为
Frida 直调微信 SendMessage(core.direct_send),不碰界面。

- 直发"确定没发出"(NOT_SENT_CODES)且开启回退时,整单交给界面自动化重新走一遍
  (同一 job_id,此时账本仍是 prepared,不会重复)。
- 直发结果不确定(超时/调用异常)时记 uncertain,绝不回退,避免一条消息发两次。
- 图片先复制到 work/direct_media:微信是异步上传 CDN 的,原文件可能被调用方删掉。
"""
import contextlib
import os
import time

from core import account_session as sessions, send_ledger


def _log(text):
    from core import reply_policy
    ticket = reply_policy._current.get()
    if ticket and ticket.get('log'):
        ticket['log'](text)


class DirectSendAdapter:
    available = True
    scope = 'direct_send'

    def __init__(self, fallback):
        self.fallback = fallback  # 界面自动化适配器(LegacySearchAdapter)

    def capability(self, chat, kind='text'):
        return '' if kind in ('text', 'image') else 'direct_payload_not_supported'

    def receipt(self, jid):
        return None

    def _use_fallback(self):
        from core import send_mode
        return bool(self.fallback) and send_mode.get()['fallback']

    def submit(self, display, chat, kind, payload, *, job_id=None, session=None,
               proactive_ticket=None):
        from core import direct_send, reply_policy
        args = (display, chat, kind, payload)
        kw = dict(job_id=job_id, session=session, proactive_ticket=proactive_ticket)
        # 语音条只能在界面里录(点「发语音」+ 虚拟麦克风),直发做不了:整单交给界面,
        # 不看「回退」开关(那个开关管的是直发失败时要不要改走界面)。
        if kind == 'voice' and self.fallback:
            return self.fallback.submit(*args, **kw)
        token = session or sessions.capture()
        jid = job_id or send_ledger.next_id()
        kw['job_id'] = jid
        ledger = send_ledger.Ledger()
        digest = send_ledger.stable_id(payload)
        row = ledger.prepare(jid, token, chat, kind, digest, payload)
        if (row['account'], row['chat'], row['kind'], row['digest']) != (
                token['account'], chat or '', kind, digest):
            return dict(ok=False, status='failed', reason='job_identity_conflict', retryable=False)
        resumable = row['status'] == 'deferred' and row['reason'] == 'reply_snapshot_unavailable'
        if resumable and not reply_policy.scoped():
            return ledger.view(row)
        if row['status'] != 'prepared' and not resumable:
            return ledger.view(row)

        # 账本还没 claim 之前先确认直发可用;不可用就整单交给界面(同一 job_id)。
        st = direct_send.status()
        if not (st.get('attached') and st.get('logged_in')):
            if self._use_fallback():
                _log(f"[发送方式] 直发不可用({st.get('error') or '未登录'}),本条改走界面自动化")
                return self.fallback.submit(*args, **kw)

        initiated = False
        try:
            if not ledger.claim(jid):
                return ledger.view(ledger.get(jid))
            if row['generation'] != token['generation'] or not sessions.valid(token):
                return ledger.view(ledger.update(jid, 'stale', 'account_session_changed'))
            reason = self.capability(chat, kind)
            if reason or not chat:
                return ledger.view(ledger.update(jid, 'not_sent', reason or 'missing_target'))
            if send_ledger.blocked_by_uncertainty(kind, chat):
                return ledger.view(ledger.update(jid, 'not_sent', 'prior_send_uncertain'))
            if not isinstance(payload, str) or not payload.strip():
                return ledger.view(ledger.update(jid, 'not_sent', 'missing_content'))
            staged = None
            if kind == 'image':
                if not os.path.isfile(payload):
                    return ledger.view(ledger.update(jid, 'not_sent', 'file_unavailable'))
                try:
                    with open(payload, 'rb') as f:
                        staged = direct_send.stage_image(f.read(), os.path.basename(payload))
                except direct_send.DirectSendError as e:
                    return ledger.view(ledger.update(jid, 'not_sent', 'direct_' + e.code))
            from core import conversation_state
            guard = (conversation_state.final_guard(proactive_ticket) if proactive_ticket
                     else contextlib.nullcontext(True))
            with guard as allowed:
                if not allowed:
                    return ledger.view(ledger.update(jid, 'not_sent', 'proactive_context_changed'))
                check_started = time.monotonic()
                decision = reply_policy.before_dispatch(chat, kind, payload, ledger, jid)
                reply_policy.timing('发送前复核', check_started)
                if decision:
                    return reply_policy.record(decision, ledger, jid)
                sessions.check(token)
                ledger.update(jid, 'initiated', 'direct_may_execute')
                initiated = True
                started = time.monotonic()
                try:
                    if kind == 'image':
                        direct_send.send_image(chat, staged, display or '', os.path.basename(payload),
                                               source='bot')
                    else:
                        direct_send.send(chat, payload, display or '', source='bot')
                except direct_send.DirectSendError as e:
                    reply_policy.timing('直发提交', started)
                    if e.code in direct_send.NOT_SENT_CODES:
                        return ledger.view(ledger.update(jid, 'not_sent', 'direct_' + e.code))
                    return ledger.view(ledger.update(jid, 'uncertain', 'direct_' + e.code))
                reply_policy.timing('直发提交', started)
            if not sessions.valid(token):
                return ledger.view(ledger.update(jid, 'uncertain', 'account_changed_after_initiation'))
            return ledger.view(ledger.update(jid, 'submitted', 'direct_submitted'))
        except Exception as exc:
            status = 'uncertain' if initiated else 'not_sent'
            reason = 'direct_exception_' + type(exc).__name__
            try:
                return ledger.view(ledger.update(jid, status, reason))
            except Exception:
                return dict(ok=False, status=status, reason=reason, job_id=jid, retryable=False)
