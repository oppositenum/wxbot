"""Keep the local message snapshot fresh while the reply worker waits on a model.

This reader never opens a chat, fetches media, calls a model or sends a message.
WeChat's durable database remains the inbox; the existing cursor consumes it.
"""
import os
import threading
import time

import config
from core import account_session as sessions, decrypt

_lock = threading.RLock()
_thread = None
_active = None
_stamp = None
_status = {'running': False, 'last_ok': 0, 'refresh_ms': 0, 'reason': 'not_started'}


def source_stamp():
    root = config.db_storage_dir()
    if not root:
        raise RuntimeError('message_source_unavailable')
    path = os.path.join(root, config.CORE_DBS['message'])
    parts = []
    for p in (path, path + '-wal'):
        try:
            st = os.stat(p)
            parts.append((st.st_ino, st.st_size, st.st_mtime_ns))
        except FileNotFoundError:
            parts.append(None)
    if parts[0] is None:
        raise RuntimeError('message_source_unavailable')
    return (path, tuple(parts))


def refresh():
    """Require a successfully refreshed, unchanged source before dispatch."""
    global _stamp
    token = sessions.check()
    with _lock:
        started = time.monotonic()
        for _ in range(2):
            stamp = (token, source_stamp())
            if stamp == _stamp:
                return
            result = decrypt.run(force=False, only=['message'])
            sessions.check(token)
            if result.get('message') not in ('ok', 'cached'):
                raise RuntimeError('message_snapshot_unavailable')
            if stamp == (token, source_stamp()):
                _stamp = stamp
                _status.update(last_ok=time.time(), refresh_ms=round((time.monotonic()-started)*1000),
                               reason='ready')
                return
        raise RuntimeError('message_source_changing')


def enabled():
    return _active is None or bool(_active())


def status():
    return dict(_status, running=bool(_thread and _thread.is_alive() and enabled()))


def start(active):
    global _thread, _active
    with _lock:
        _active = active
        if _thread and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name='reply-inbox', daemon=True)
        _thread.start()


def _loop(stop=None):
    stop = stop or threading.Event()
    while not stop.is_set():
        if enabled():
            try:
                with sessions.bind():
                    refresh()
            except Exception as exc:
                _status.update(reason=type(exc).__name__)
        stop.wait(0.35)
