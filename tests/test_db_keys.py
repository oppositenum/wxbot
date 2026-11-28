import json
import os
import tempfile
import unittest
from unittest.mock import patch

from core import db_keys

GOOD = "a" * 64
BAD = "b" * 64


class DbKeysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = os.path.join(self.tmp.name, "db_storage")
        for rel in ("message/message_0.db", "contact/contact.db", "session/session.db", "message/media_0.db"):
            os.makedirs(os.path.dirname(os.path.join(self.st, rel)), exist_ok=True)
            open(os.path.join(self.st, rel), "wb").write(b"\0" * 4096)
        self.keys = os.path.join(self.tmp.name, "acct", "keys.json")
        self.p = [patch("config.keys_json", return_value=self.keys),
                  patch.object(db_keys, "page_ok", side_effect=lambda k, p: k == GOOD)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.tmp.cleanup()

    def write(self, d):
        os.makedirs(os.path.dirname(self.keys), exist_ok=True)
        json.dump(d, open(self.keys, "w"))

    def test_failed_scan_never_wipes_existing_keys(self):
        self.write({"message/message_0.db": GOOD, "contact/contact.db": GOOD, "session/session.db": GOOD,
                    "_img_key": "img"})
        scan = os.path.join(self.tmp.name, "scan.json")
        json.dump({"message/message_0.db": BAD}, open(scan, "w"))     # 扫描结果是错的
        with patch.object(db_keys, "hook_supported", return_value=False):
            ok, need = db_keys.refresh(self.st, linux_keys_out=scan, log=lambda m: None)
        d = db_keys.load()
        self.assertTrue(ok)
        self.assertEqual((d["message/message_0.db"], d["_img_key"]), (GOOD, "img"))
        self.assertEqual(need, ["message/media_0.db"])

    def test_missing_keys_are_captured_with_hook_and_merged(self):
        self.write({"_img_key": "img"})
        seen = {}

        def cap(rels, st, secs, trigger, log):
            seen["rels"] = rels
            return {r: GOOD for r in rels}
        with patch.object(db_keys, "hook_supported", return_value=True), \
                patch.object(db_keys, "capture", side_effect=cap):
            ok, need = db_keys.refresh(self.st, trigger=False, log=lambda m: None)
        self.assertTrue(ok)
        self.assertEqual(need, [])
        self.assertEqual(set(seen["rels"]), {"message/message_0.db", "contact/contact.db", "session/session.db",
                                             "message/media_0.db"})
        d = db_keys.load()
        self.assertEqual((d["contact/contact.db"], d["_img_key"]), (GOOD, "img"))
        self.assertTrue(oct(os.stat(self.keys).st_mode).endswith("600"))
        self.assertTrue([n for n in os.listdir(os.path.dirname(self.keys)) if ".bak-" in n])

    def test_core_missing_reports_not_ok(self):
        self.write({})
        with patch.object(db_keys, "hook_supported", return_value=False):
            ok, need = db_keys.refresh(self.st, log=lambda m: None)
        self.assertFalse(ok)
        self.assertIn("message/message_0.db", need)


if __name__ == "__main__":
    unittest.main()


class ImgKeyTest(unittest.TestCase):
    def test_img_key_validated_against_dat_and_saved_as_hex(self):
        from Crypto.Cipher import AES
        from core import imgdec
        key = b"0123456789abcdef"
        plain = b"\xff\xd8\xff\xe0" + b"x" * 40
        n = (len(plain) // 16 + 1) * 16
        padded = plain + bytes([n - len(plain)]) * (n - len(plain))
        dat = imgdec.SIG + len(plain).to_bytes(4, "little") + (0).to_bytes(4, "little") + b"\0" + \
            AES.new(key, AES.MODE_ECB).encrypt(padded)
        self.assertTrue(db_keys.img_key_ok(key, [dat]))
        self.assertFalse(db_keys.img_key_ok(b"f" * 16, [dat]))
        self.assertEqual(db_keys.img_key_text(key), key.hex())
        self.assertEqual(imgdec._key_bytes(db_keys.img_key_text(key)), key)
