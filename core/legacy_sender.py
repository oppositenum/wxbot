"""Original Linux search/paste/Enter transport, explicitly selected by the owner.

An OK from the historical script means UI submission, not verified delivery.
Keep durable single-attempt accounting without invoking the later OCR adapter.
"""
import contextlib
import os
import uuid
import time

from core import account_session as sessions, docker_wx, send_ledger


class LegacySearchAdapter:
    available = True
    scope = 'legacy_search'
    script = '/usr/local/bin/wx_send_legacy.py'

    def capability(self, chat, kind='text'):
        return '' if kind in ('text', 'image') else 'legacy_payload_not_supported'

    def receipt(self, jid):
        return None

    def submit(self, display, chat, kind, payload, *, job_id=None, session=None,
               proactive_ticket=None):
        token = session or sessions.capture()
        jid = job_id or send_ledger.next_id()
        ledger = send_ledger.Ledger()
        digest = send_ledger.stable_id(payload)
        row = ledger.prepare(jid, token, chat, kind, digest, payload)
        if (row['account'], row['chat'], row['kind'], row['digest']) != (
                token['account'], chat or '', kind, digest):
            return dict(ok=False, status='failed', reason='job_identity_conflict', retryable=False)
        # Neither successful submissions nor interrupted attempts are replayed.
        resumable = row['status'] == 'deferred' and row['reason'] == 'reply_snapshot_unavailable'
        if resumable:
            from core import reply_policy
            if not reply_policy.scoped():
                return ledger.view(row)
        if row['status'] != 'prepared' and not resumable:
            return ledger.view(row)
        initiated = False
        docker_wx.request_priority()
        from core import reply_policy
        wait_started = time.monotonic()
        try:
            with docker_wx.UI_LOCK:
                reply_policy.timing('等待微信界面', wait_started)
                if not ledger.claim(jid):
                    return ledger.view(ledger.get(jid))
                if row['generation'] != token['generation'] or not sessions.valid(token):
                    return ledger.view(ledger.update(jid, 'stale', 'account_session_changed'))
                reason = self.capability(chat, kind)
                if reason or not isinstance(display, str) or not display.strip():
                    return ledger.view(ledger.update(jid, 'not_sent', reason or 'missing_target'))
                if send_ledger.blocked_by_uncertainty(kind, chat):
                    return ledger.view(ledger.update(jid, 'not_sent', 'prior_send_uncertain'))
                if not isinstance(payload, str) or not payload.strip():
                    return ledger.view(ledger.update(jid, 'not_sent', 'missing_content'))
                # Resolve the selected contact's searchable WeChat ID locally.
                # Groups and contacts without an alias retain the original name.
                from core.sender import search_key
                query, _ = search_key(chat, display)
                arg = payload
                if kind == 'image':
                    if not os.path.isfile(payload):
                        return ledger.view(ledger.update(jid, 'not_sent', 'file_unavailable'))
                    if not docker_wx.LOCAL:
                        arg = '/tmp/wxlegacy_' + uuid.uuid4().hex + os.path.splitext(payload)[1]
                        copied = docker_wx._docker('cp', payload, docker_wx.CONTAINER + ':' + arg)
                        if copied.returncode:
                            return ledger.view(ledger.update(jid, 'not_sent', 'image_copy_failed'))
                from core import conversation_state
                guard = conversation_state.final_guard(proactive_ticket) if proactive_ticket else contextlib.nullcontext(True)
                with guard as allowed:
                    if not allowed:
                        return ledger.view(ledger.update(jid, 'not_sent', 'proactive_context_changed'))
                    check_started = time.monotonic()
                    decision = reply_policy.before_dispatch(chat, kind, payload, ledger, jid)
                    reply_policy.timing('发送前复核', check_started)
                    if decision:
                        return reply_policy.record(decision, ledger, jid)
                    sessions.check(token)
                    ledger.update(jid, 'initiated', 'legacy_ui_may_execute')
                    initiated = True
                    ui_started = time.monotonic()
                    result = docker_wx._exec('python3', self.script, kind, query, arg,
                                             timeout=40 if kind == 'text' else 60)
                    reply_policy.timing('微信界面提交', ui_started)
                if not sessions.valid(token):
                    return ledger.view(ledger.update(jid, 'uncertain', 'account_changed_after_initiation'))
                if result.returncode == 0 and 'OK' in result.stdout.splitlines():
                    return ledger.view(ledger.update(jid, 'submitted', 'legacy_ui_submitted'))
                if result.stdout.strip() == 'ERR:no-window':
                    return ledger.view(ledger.update(jid, 'not_sent', 'legacy_no_window'))
                return ledger.view(ledger.update(jid, 'uncertain', 'legacy_ui_result_unknown'))
        except Exception as exc:
            status = 'uncertain' if initiated else 'not_sent'
            reason = 'legacy_exception_' + type(exc).__name__
            try:
                return ledger.view(ledger.update(jid, status, reason))
            except Exception:
                return dict(ok=False, status=status, reason=reason, job_id=jid, retryable=False)
        finally:
            docker_wx.release_priority()
