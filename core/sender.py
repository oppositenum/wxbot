"""Durable send coordinator using the owner's selected original Linux transport.

Legacy UI submission is distinct from a verified receipt. The later native
adapter remains available for isolated checks; it is not the active sender.
An interrupted send is never repeated automatically.
"""
import os
import json
import time
import contextlib
from core import docker_wx, account_session as sessions, send_ledger

# One reentrant lock, shared with navigation, harvest and key capture. No second
# sender lock: avoids UI_LOCK -> SEND_LOCK inversion in the harvest path.
SEND_LOCK = docker_wx.UI_LOCK


class UnavailableIdentityAdapter:
    available = False


from core.native_sender import NativeContactAdapter
from core.legacy_sender import LegacySearchAdapter
_adapter = LegacySearchAdapter()


def preflight(chat, session=None, kind='text'):
    """Cheap capability check before paid generation, never recipient proof.

    Actual account and recipient checks still run in _send immediately before UI
    actions. None means generation may proceed, not that a message was sent.
    """
    token = session or sessions.capture()
    reason = ('account_session_changed' if not sessions.valid(token) else
              'cannot_confirm_target' if not chat or not _adapter.available else '')
    if not reason and hasattr(_adapter, 'capability'):
        reason = _adapter.capability(chat, kind)
    if reason:
        return dict(ok=False, status='stale' if reason == 'account_session_changed' else 'not_sent',
                    reason=reason, retryable=False,
                    message='本次未生成、未发送：' + {
                        'account_session_changed':'账号已变化',
                        'native_group_identity_unavailable':'当前适配器尚不能确认群聊身份',
                        'native_payload_not_supported':'当前适配器暂只支持私聊文字',
                        'legacy_payload_not_supported':'最早版支持文字和图片，不支持群成员定向 @',
                        'native_contact_identity_unavailable':'联系人缺少可唯一核对的微信号',
                        'native_client_state_unavailable':'当前微信账号或窗口状态无法核对',
                    }.get(reason, '真实微信发送适配器尚不可用，无法确认收件人'))
    return None


def search_key(chat_username, display_name):
    """把"要发给谁"解析成 (搜索词, 结果行显示名)。

    重名克星：remark 可能多个联系人重名(如 6 个"老婆")，搜它会出多行、开错人。
    而【微信号 alias 全局唯一且可被搜索框命中】，故优先按 wxid 反查出 alias 去搜，
    把结果收敛到唯一一行；结果行显示的仍是 remark/昵称，故用 display_name 定位点击。
    群名和显示名都只用于导航，不能作为收件人身份依据。
    """
    if not chat_username or chat_username.endswith("@chatroom"):
        return display_name, display_name
    try:
        from core import db
        con = db.connect("contact")
        try:
            r = con.execute("SELECT alias, remark, nick_name FROM contact "
                            "WHERE username=?", (chat_username,)).fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return display_name, display_name
    if r:
        alias = (r["alias"] or "").strip()
        locate = display_name or (r["remark"] or "").strip() or (r["nick_name"] or "").strip()
        if alias and not alias.startswith("wxid_"):
            return alias, (locate or alias)         # 搜唯一微信号，按显示名定位行
    return display_name, display_name


def _vision_open(display_name, chat_username=None):
    """Navigation only. Its return value is NEVER recipient proof."""
    with SEND_LOCK:
        docker_wx.clear_open()
        query, _ = search_key(chat_username, display_name)
        ok = docker_wx.open_chat(query)
        if ok and chat_username:
            docker_wx.note_open(chat_username)  # navigation hint only; _send never reads it
        return ok


def focus_chat(display_name, chat_username):
    if not chat_username or not SEND_LOCK.acquire(blocking=False):
        return False
    try:
        if docker_wx.current_open() == chat_username:
            return True  # suppress redundant navigation, not recipient confirmation
        return _vision_open(display_name, chat_username)
    finally:
        SEND_LOCK.release()


def _identity_matches(proof, token, chat):
    return (isinstance(proof, dict) and proof.get('trusted') is True
            and proof.get('account') == token['account']
            and proof.get('generation') == token['generation']
            and proof.get('chat') == chat and bool(proof.get('session_identity')))


def _receipt_matches(receipt, token, chat, jid):
    return (_identity_matches(receipt, token, chat)
            and receipt.get('job_id') == jid and bool(receipt.get('message_id')))


def _send(display, chat, kind, payload, *, job_id=None, session=None, proactive_ticket=None):
    if isinstance(_adapter, LegacySearchAdapter):
        return _adapter.submit(display, chat, kind, payload, job_id=job_id, session=session,
                               proactive_ticket=proactive_ticket)
    token = session or sessions.capture()
    jid = job_id or send_ledger.next_id()
    ledger = send_ledger.Ledger()
    digest = send_ledger.stable_id(payload)
    row = ledger.prepare(jid, token, chat, kind, digest, payload)
    if (row['account'], row['chat'], row['kind'], row['digest']) != (
            token['account'], chat or '', kind, digest):
        return dict(ledger.view(row), ok=False, reason='job_identity_conflict',
                    error='job_identity_conflict', retryable=False)
    if proactive_ticket is not None:
        ledger.bind_proactive(jid, proactive_ticket)
    proactive_ticket = ledger.proactive(jid)
    # Initiated and terminal rows never go through send again, even after restart.
    resumable = row['status'] == 'deferred' and row['reason'] == 'reply_snapshot_unavailable'
    if resumable:
        from core import reply_policy
        if not reply_policy.scoped():
            return ledger.view(row)
    if row['status'] not in ('prepared', 'not_sent') and not resumable:
        return ledger.view(row)
    if row['status'] == 'not_sent' and row['reason'] != 'open_failed':
        return ledger.view(row)
    if row['generation'] != token['generation'] or not sessions.valid(token):
        return ledger.view(ledger.update(jid, 'stale', 'account_session_changed'))
    docker_wx.request_priority()
    initiated = False
    staged = False
    SEND_LOCK.acquire()  # Keep ownership through staging cleanup as well.
    try:
        with SEND_LOCK:
            if not ledger.claim(jid):
                return ledger.view(ledger.get(jid))
            if not sessions.valid(token):
                return ledger.view(ledger.update(jid, 'stale', 'account_session_changed'))
            if send_ledger.blocked_by_uncertainty(kind, chat):
                return ledger.view(ledger.update(jid, 'not_sent', 'prior_send_uncertain'))
            if not chat or not _adapter.available:
                return ledger.view(ledger.update(jid, 'not_sent', 'cannot_confirm_target'))
            if hasattr(_adapter, 'capability'):
                reason = _adapter.capability(chat, kind)
                if reason:
                    return ledger.view(ledger.update(jid, 'not_sent', reason))
            if kind == 'image' and not os.path.isfile(payload):
                return ledger.view(ledger.update(jid, 'failed', 'file_unavailable'))
            # Adapter.open may navigate, but must NEVER input/send message content.
            if not _adapter.open(display, chat):
                reason = getattr(_adapter, 'last_reason', '')
                if reason:
                    return ledger.view(ledger.update(jid, 'not_sent', reason))
                return ledger.view(ledger.update(jid, 'not_sent', 'open_failed', time.time()+30))
            if not _identity_matches(_adapter.identity(), token, chat):
                return ledger.view(ledger.update(jid, 'not_sent', 'cannot_confirm_target'))
            if not sessions.valid(token):
                return ledger.view(ledger.update(jid, 'stale', 'account_session_changed'))
            if not _identity_matches(_adapter.identity(), token, chat):
                return ledger.view(ledger.update(jid, 'not_sent', 'target_changed'))
            if hasattr(_adapter, 'prepare_send'):
                staged = True
                _adapter.prepare_send(kind, payload, jid)
                if not sessions.valid(token) or not _identity_matches(_adapter.identity(), token, chat):
                    return ledger.view(ledger.update(jid, 'not_sent', 'target_changed_after_staging'))
            from core import conversation_state
            guard = conversation_state.final_guard(proactive_ticket) if proactive_ticket else contextlib.nullcontext(True)
            with guard as allowed:
                if not allowed:
                    return ledger.view(ledger.update(jid, 'not_sent', 'proactive_context_changed'))
                from core import reply_policy
                decision = reply_policy.before_dispatch(chat, kind, payload, ledger, jid)
                if decision:
                    return reply_policy.record(decision, ledger, jid)
                # Durable BEFORE handing control to any code that might click send.
                ledger.update(jid, 'initiated', 'send_may_execute')
                initiated = True
                _adapter.send(kind, payload, jid)
            if not sessions.valid(token):
                return ledger.view(ledger.update(jid, 'uncertain', 'account_changed_after_initiation'))
            receipt = _adapter.receipt(jid)  # read-only; missing/delayed != not sent
            if _receipt_matches(receipt, token, chat, jid):
                return ledger.view(ledger.update(jid, 'confirmed', 'observed_new_target_message'))
            return ledger.view(ledger.update(jid, 'uncertain', 'receipt_not_available'))
    except Exception as exc:
        status = 'uncertain' if initiated else 'not_sent'
        reason = ('send_exception_' if initiated else 'pre_send_exception_') + type(exc).__name__
        if isinstance(exc, RuntimeError) and str(exc) in (
                'existing_draft', 'input_verification_failed', 'target_changed',
                'target_changed_after_staging', 'target_changed_before_click',
                'focus_changed', 'client_account_mismatch', 'native_ui_failed'):
            reason = ('after_initiation_' if initiated else 'before_send_') + str(exc)
        try:
            return ledger.view(ledger.update(jid, status, reason))
        except Exception:
            # Disk failure after send leaves durable initiated; restart sees uncertain.
            return {'ok': False, 'status': status, 'reason': reason + '_ledger_update_failed',
                    'error': reason + '_ledger_update_failed', 'job_id': jid, 'retryable': False}
    finally:
        if staged and not initiated and hasattr(_adapter, 'cancel_staging'):
            try:
                with SEND_LOCK:
                    _adapter.cancel_staging(payload)
            except Exception:
                pass  # Do not destroy a draft after target/account has changed.
        SEND_LOCK.release()
        docker_wx.release_priority()


def retry(job_id, session=None, display_name=None):
    """Resume proven pre-send failures; snapshot deferral requires a reply scope.

    A UI timeout or uncertain submission is never eligible.
    """
    ledger = send_ledger.Ledger()
    row = ledger.get(job_id)
    if not row:
        return {'ok': False, 'status': 'failed', 'reason': 'job_not_found'}
    if not ledger.view(row).get('retryable') or row['payload'] is None:
        return ledger.view(row)
    token = session or sessions.capture()
    # Stable ID is used as navigation hint; trusted adapter still must resolve it.
    return _send(display_name or row['chat'], row['chat'], row['kind'], json.loads(row['payload']),
                 job_id=job_id, session=token)


def reconcile(job_id, session=None):
    """Only read a correlated receipt; never navigate, input or click send."""
    ledger = send_ledger.Ledger()
    row = ledger.get(job_id)
    if not row:
        return {'ok': False, 'status': 'unknown', 'reason': 'job_not_found'}
    token = session or sessions.capture()
    if (row['status'] in ('initiated', 'uncertain') and sessions.valid(token)
            and row['account'] == token['account'] and row['generation'] == token['generation']
            and _adapter.available):
        receipt = _adapter.receipt(job_id)
        if _receipt_matches(receipt, token, row['chat'], job_id):
            row = ledger.update(job_id, 'confirmed', 'reconciled_target_receipt')
    return ledger.view(row)


def send_text(display_name, text, chat_username=None, retries=2, verify=True, **context):
    return _send(display_name, chat_username, 'text', text, **context)


def send_relay(display_name, text, chat_username=None, retries=2, **context):
    return send_text(display_name, text, chat_username, **context)


def send_image(display_name, host_path, chat_username=None, retries=1, verify=True, **context):
    return _send(display_name, chat_username, 'image', host_path, **context)


def send_at(display_name, chat_username, member_wxid, member_display, text, retries=2, **context):
    return _send(display_name, chat_username, 'at',
                 {'member': member_wxid, 'display': member_display, 'text': text}, **context)


def send_webhook(url, payload, *, job_id, session):
    """Push HTTP transport: confirmed means HTTP 2xx, NOT WeChat delivery.
    Same durable intent rule; timeout/non-2xx after request initiation is held.
    """
    import urllib.parse
    import urllib.request
    ledger = send_ledger.Ledger()
    target = 'webhook:' + send_ledger.stable_id(url)
    digest = send_ledger.stable_id(url, payload)
    row = ledger.prepare(job_id, session, target, 'webhook', digest)
    if row['digest'] != digest or row['account'] != session['account']:
        return {'ok': False, 'status': 'failed', 'reason': 'job_identity_conflict', 'job_id': job_id}
    if row['status'] != 'prepared':
        return ledger.view(row)
    initiated = False
    try:
        with SEND_LOCK:
            if not ledger.claim(job_id):
                return ledger.view(ledger.get(job_id))
            if row['generation'] != session['generation'] or not sessions.valid(session):
                return ledger.view(ledger.update(job_id, 'stale', 'account_session_changed'))
            if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
                return ledger.view(ledger.update(job_id, 'not_sent', 'invalid_webhook_target'))
            request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                headers={'Content-Type': 'application/json'}, method='POST')
            ledger.update(job_id, 'initiated', 'http_request_may_execute')
            initiated = True
            with urllib.request.urlopen(request, timeout=10) as response:
                accepted = 200 <= response.getcode() < 300
            if not sessions.valid(session):
                return ledger.view(ledger.update(job_id, 'uncertain', 'account_changed_after_initiation'))
            status, reason = ('confirmed', 'http_2xx_received') if accepted else ('uncertain', 'http_not_accepted')
            return ledger.view(ledger.update(job_id, status, reason))
    except Exception as exc:
        status = 'uncertain' if initiated else 'not_sent'
        reason = 'http_exception_' + type(exc).__name__
        try:
            return ledger.view(ledger.update(job_id, status, reason))
        except Exception:
            return {'ok': False, 'status': status, 'reason': reason + '_ledger_update_failed',
                    'job_id': job_id, 'retryable': False}
