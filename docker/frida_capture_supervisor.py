#!/usr/bin/env python3
"""Supervise a media-file observer attached to the local WeChat process only."""
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

ROOT = Path(os.environ.get('WXBOT_CAPTURE_ROOT', '/root/wxbot_capture'))
AGENT = Path(__file__).resolve().with_name('frida_capture.js')
STOP = threading.Event()


def write_status(**state):
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = ROOT / 'status.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(dict(updated_at=time.time(), supervisor_pid=os.getpid(), **state)))
    tmp.chmod(0o600)
    tmp.replace(path)


def find_wechat(dev):
    matches = [p for p in dev.enumerate_processes() if p.name == 'wechat']
    # Ambiguous ownership never attaches to an arbitrary process.
    return matches[0].pid if len(matches) == 1 else None


def run():
    import frida
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = open(ROOT / '.supervisor.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    session = script = None
    current_pid = None
    probe = None
    detached = threading.Event()
    errors = []
    def on_message(message, _data):
        if message.get('type') == 'error':
            errors.append('agent_runtime_error')
    try:
        dev = frida.get_local_device()
        source = AGENT.read_text()
        while not STOP.is_set():
            try:
                pid = find_wechat(dev)
            except Exception as exc:
                write_status(state='disconnected', reason=type(exc).__name__)
                detached.set()
                STOP.wait(5)
                continue
            if session is None or detached.is_set() or pid != current_pid:
                if session:
                    try: session.detach()
                    except Exception: pass
                session = script = None
                detached.clear()
                if pid is None:
                    write_status(state='waiting_for_wechat')
                    STOP.wait(3)
                    continue
                try:
                    errors.clear()
                    probe = None
                    write_status(state='attaching', wechat_pid=pid)
                    session = dev.attach(pid)
                    session.on('detached', lambda *_: detached.set())
                    script = session.create_script(source)
                    script.on('message', on_message)
                    script.load()
                    current_pid = pid
                    if '--selftest' in sys.argv:
                        probe = script.exports_sync.selftest()
                except Exception as exc:
                    if session:
                        try: session.detach()
                        except Exception: pass
                    session = script = None
                    write_status(state='attach_failed', reason=type(exc).__name__)
                    STOP.wait(5)
                    continue
            try:
                stats = script.exports_sync.status()
                healthy = 'close' in stats['hooks'] and any(h.startswith('open') for h in stats['hooks'])
                write_status(state='ready' if healthy and not errors else 'degraded', wechat_pid=pid,
                             frida_version=frida.__version__, selftest=probe, **stats)
            except Exception as exc:
                write_status(state='disconnected', reason=type(exc).__name__)
                detached.set()
            STOP.wait(2)
    finally:
        if session:
            try: session.detach()
            except Exception: pass
        write_status(state='stopped')
        lock.close()


if __name__ == '__main__':
    run()
