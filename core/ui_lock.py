"""Thread-reentrant + process-wide UI ownership for local WeChat automation."""
import fcntl
import os
import threading
import time


class UILock:
    def __init__(self, path='/tmp/wxbot-wechat-ui.lock'):
        self.path = path
        self.lock = threading.RLock()
        self.local = threading.local()

    def acquire(self, blocking=True, timeout=-1):
        started = time.monotonic()
        if not self.lock.acquire(blocking, timeout):
            return False
        if getattr(self.local, 'depth', 0):
            self.local.depth += 1
            return True
        fd = None
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not blocking or (timeout >= 0 and time.monotonic() - started >= timeout):
                        os.close(fd); self.lock.release(); return False
                    time.sleep(.05)
            self.local.fd, self.local.depth = fd, 1
            return True
        except BaseException:
            if fd is not None:
                os.close(fd)
            self.lock.release()
            raise

    def release(self):
        if not getattr(self.local, 'depth', 0):
            raise RuntimeError('UI lock not owned')
        self.local.depth -= 1
        if not self.local.depth:
            fcntl.flock(self.local.fd, fcntl.LOCK_UN)
            os.close(self.local.fd)
        self.lock.release()

    def __enter__(self):
        self.acquire(); return self

    def __exit__(self, *args):
        self.release()
