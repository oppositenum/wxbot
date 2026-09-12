"""Task-local account epochs. Observed switches and process restarts invalidate work.

The legacy mtime detector is NOT a trusted WeChat login-session identity. Unobserved
A->B->A transitions cannot be inferred from it; the UI adapter therefore fails closed.
"""
import contextlib
import contextvars
import functools
import json
import os
import tempfile
import threading
import uuid
import time

import config

_lock = threading.RLock()
_current = None
_identity_probe = None
_identity_stamp = None
_identity_checked = float('-inf')
_bound = contextvars.ContextVar('wxbot_account_session', default=None)


class StaleAccount(RuntimeError):
    pass


def observe():
    global _current, _identity_stamp, _identity_checked
    with _lock:
        account = config.wxid() or 'default'
        changed = False
        if _identity_probe and time.monotonic() - _identity_checked > 0.5:
            try:
                stamp = _identity_probe()
            except Exception:
                stamp = 'native_client_unavailable'
            changed = _identity_stamp is not None and stamp != _identity_stamp
            _identity_stamp = stamp
            _identity_checked = time.monotonic()
        if _current is None or _current['account'] != account or changed:
            _current = {'account': account, 'generation': uuid.uuid4().hex}
        return dict(_current)


def install_identity_probe(probe):
    """Use existing live process/account metadata; never capture keys or inject."""
    global _identity_probe, _identity_stamp, _identity_checked
    with _lock:
        _identity_probe = probe
        _identity_stamp = None
        _identity_checked = float('-inf')
        observe()


def refresh_identity():
    global _identity_checked
    with _lock:
        _identity_checked = float('-inf')
        return observe()


def capture():
    return dict(_bound.get() or observe())


def valid(token):
    return bool(token and token == observe() and token.get('account') != 'default')


def check(token=None):
    token = token or capture()
    if not valid(token):
        raise StaleAccount('account_session_changed')
    return token


@contextlib.contextmanager
def bind(token=None):
    token = token or capture()
    check(token)
    mark = _bound.set(dict(token))
    try:
        yield token
    finally:
        _bound.reset(mark)


def task(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with bind():
            result = fn(*args, **kwargs)
            # A send outcome must survive a concurrent switch. Its ledger already
            # binds it to the original account; do not flatten uncertain into error.
            if isinstance(result, dict) and result.get('status') in (
                    'confirmed', 'submitted', 'uncertain', 'not_sent', 'failed', 'stale', 'skipped', 'deferred'):
                return result
            check()
            return result
    return wrapped


def bound_root():
    token = _bound.get()
    if token is None:
        return None
    check(token)
    return os.path.join(config.ACCOUNTS_DIR, token['account'])


def atomic_json(path, value, token=None):
    """Pinned path, fsync + replace; serialize this process's guarded writes."""
    token = token or capture()
    with _lock:
        check(token)
        root = os.path.realpath(os.path.join(config.ACCOUNTS_DIR, token['account']))
        if os.path.commonpath([root, os.path.realpath(path)]) != root:
            raise StaleAccount('account_path_mismatch')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.send-write-', dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(value, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            check(token)
            os.replace(tmp, path)
            dfd = os.open(os.path.dirname(path), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
