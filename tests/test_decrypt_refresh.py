import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from core import decrypt


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "a.db")
        self.dst = os.path.join(self.tmp.name, "out", "a.db")
        with open(self.src, "wb") as f:
            f.write(b"x" * 4096)
        decrypt._failed.clear()
        self.sleep = patch("time.sleep").start()

    def tearDown(self):
        patch.stopall()
        self.tmp.cleanup()

    def _fail(self, *a):
        raise ValueError("file is not a database")

    def test_failure_backs_off_until_source_changes(self):
        with patch.object(decrypt, "decrypt_db", side_effect=self._fail) as d:
            self.assertTrue(decrypt._refresh_one(self.src, "00", self.dst, False).startswith("err:"))
            self.assertEqual(d.call_count, decrypt.RETRIES)
            self.assertEqual(decrypt._refresh_one(self.src, "00", self.dst, False), "err:backoff")
            self.assertEqual(d.call_count, decrypt.RETRIES)          # 源库没变:不重跑
            with open(self.src, "ab") as f:
                f.write(b"y")                                       # 源库变了:马上再试
            decrypt._refresh_one(self.src, "00", self.dst, False)
            self.assertEqual(d.call_count, 2 * decrypt.RETRIES)

    def test_backoff_expires(self):
        with patch.object(decrypt, "decrypt_db", side_effect=self._fail) as d:
            decrypt._refresh_one(self.src, "00", self.dst, False)
            stamp, t = decrypt._failed[self.dst]
            decrypt._failed[self.dst] = (stamp, t - decrypt.FAIL_BACKOFF - 1)
            decrypt._refresh_one(self.src, "00", self.dst, False)
            self.assertEqual(d.call_count, 2 * decrypt.RETRIES)

    def test_success_clears_failure(self):
        def ok(src, key, dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            open(dst, "wb").close()
        decrypt._failed[self.dst] = ((0, 0), time.time())
        with patch.object(decrypt, "decrypt_db", side_effect=ok):
            self.assertEqual(decrypt._refresh_one(self.src, "00", self.dst, False), "ok")
        self.assertNotIn(self.dst, decrypt._failed)

    def test_busy_database_does_not_block_another(self):
        other = os.path.join(self.tmp.name, "out", "b.db")
        os.makedirs(os.path.dirname(other), exist_ok=True)
        open(other, "wb").close()
        os.utime(other, (time.time() + 60, time.time() + 60))       # b 已是最新
        held = threading.Event()
        release = threading.Event()

        def hold():
            with decrypt._lock_for(self.dst):
                held.set()
                release.wait(5)
        t = threading.Thread(target=hold)
        t.start()
        held.wait(5)
        try:
            started = time.monotonic()
            self.assertEqual(decrypt._refresh_one(self.src, "00", other, False), "cached")
            self.assertLess(time.monotonic() - started, 1)
        finally:
            release.set()
            t.join()


if __name__ == "__main__":
    unittest.main()
