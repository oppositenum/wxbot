"""Webhook 订阅:加密、过滤、投递、配置存储。"""
import base64
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from core import webhook


class Crypto(unittest.TestCase):
    def test_roundtrip_and_tamper(self):
        sec = webhook.new_secret()
        self.assertEqual(len(sec), 64)
        env = webhook.encrypt(sec, "evt_1", 1700000000, {"content": "你好"})
        self.assertNotIn("你好", json.dumps(env, ensure_ascii=False))
        self.assertEqual(webhook.decrypt(sec, env)["content"], "你好")
        for k, v in (("id", "evt_2"), ("ts", 1700000001)):                    # id/ts 进了 AAD,改了就解不开
            with self.assertRaises(Exception):
                webhook.decrypt(sec, {**env, k: v})
        data = bytearray(base64.b64decode(env["data"])); data[0] ^= 1
        with self.assertRaises(Exception):
            webhook.decrypt(sec, {**env, "data": base64.b64encode(bytes(data)).decode()})
        with self.assertRaises(Exception):
            webhook.decrypt(webhook.new_secret(), env)
        with self.assertRaises(ValueError):
            webhook.decrypt(sec, env, max_age=300)                              # 太旧:当重放

    def test_iv_unique(self):
        sec = webhook.new_secret()
        self.assertNotEqual(webhook.encrypt(sec, "e", 1, {})["iv"], webhook.encrypt(sec, "e", 1, {})["iv"])


class Filters(unittest.TestCase):
    def test_wants(self):
        h = {"all_chats": True, "groups": True, "private": True, "include_self": False, "categories": []}
        self.assertTrue(webhook.wants(h, "wxid_a", {"category": "text"}))
        self.assertFalse(webhook.wants(h, "wxid_a", {"is_self": True}))
        self.assertFalse(webhook.wants({**h, "groups": False}, "1@chatroom", {}))
        self.assertFalse(webhook.wants({**h, "private": False}, "wxid_a", {}))
        self.assertFalse(webhook.wants({**h, "all_chats": False, "sources": ["wxid_b"]}, "wxid_a", {}))
        self.assertTrue(webhook.wants({**h, "all_chats": False, "sources": ["wxid_a"]}, "wxid_a", {}))
        self.assertFalse(webhook.wants({**h, "categories": ["image"]}, "wxid_a", {"category": "text"}))
        self.assertTrue(webhook.wants({**h, "categories": ["image"]}, "wxid_a", {"category": "image"}))

    def test_sources_needed(self):
        self.assertIsNone(webhook.sources_needed([{"all_chats": True}]))
        self.assertEqual(webhook.sources_needed([{"all_chats": False, "sources": ["a"]},
                                                 {"all_chats": False, "sources": ["b"]}]), {"a", "b"})

    def test_event_id_stable_per_message_and_hook(self):
        a = webhook.event_id("me", "c", 5, "wh_1")
        self.assertEqual(a, webhook.event_id("me", "c", 5, "wh_1"))
        self.assertNotEqual(a, webhook.event_id("me", "c", 5, "wh_2"))
        self.assertNotEqual(a, webhook.event_id("me", "c", 6, "wh_1"))


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = patch.object(webhook.config, "account_dir", return_value=self.tmp.name)
        p.start(); self.addCleanup(p.stop); self.addCleanup(self.tmp.cleanup)

    def test_crud_secret_hidden_and_file_private(self):
        h = webhook.create({"name": "订单系统", "url": "https://example.com/hook"})
        self.assertEqual(len(h["secret"]), 64)
        listed = webhook.list_hooks()[0]
        self.assertNotIn("secret", listed)
        self.assertEqual(listed["secret_hint"], "…" + h["secret"][-4:])
        mode = stat.S_IMODE(os.stat(os.path.join(self.tmp.name, webhook.FILE)).st_mode)
        self.assertEqual(mode, 0o600)
        with self.assertRaises(ValueError):
            webhook.update(h["id"], {"url": "ftp://x"})
        webhook.update(h["id"], {"all_chats": False, "sources": ["wxid_a", "wxid_a"], "enabled": False})
        self.assertEqual(webhook.get(h["id"])["sources"], ["wxid_a"])
        self.assertEqual(webhook.active(), [])
        new = webhook.rotate(h["id"])["secret"]
        self.assertNotEqual(new, h["secret"])
        webhook.delete(h["id"])
        self.assertEqual(webhook.list_hooks(), [])
        with self.assertRaises(ValueError):
            webhook.create({"url": "not a url"})


class Receiver(BaseHTTPRequestHandler):
    got, codes = [], []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Receiver.got.append((dict(self.headers), json.loads(body)))
        code = Receiver.codes.pop(0) if Receiver.codes else 200
        self.send_response(code); self.end_headers()

    def log_message(self, *a):
        pass


class Delivery(unittest.TestCase):
    def setUp(self):
        Receiver.got, Receiver.codes = [], []
        self.srv = HTTPServer(("127.0.0.1", 0), Receiver)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.hook = {"id": "wh_t", "url": f"http://127.0.0.1:{self.srv.server_port}/in", "secret": webhook.new_secret()}

    def test_receiver_can_decrypt_and_retry_keeps_id(self):
        Receiver.codes = [500, 200]
        ok = webhook._deliver(self.hook, "message", "evt_x", {"message": {"content": "秘密"}}, None,
                              sleep=lambda s: None, retry_delays=(0, 1, 1))
        self.assertTrue(ok)
        self.assertEqual(len(Receiver.got), 2)
        for headers, env in Receiver.got:
            self.assertEqual(headers["X-Wxbot-Id"], "evt_x")
            self.assertEqual(headers["X-Wxbot-Timestamp"], str(env["ts"]))
            self.assertNotIn("秘密", json.dumps(env, ensure_ascii=False))
            self.assertEqual(webhook.decrypt(self.hook["secret"], env, max_age=60)["message"]["content"], "秘密")
        self.assertNotEqual(Receiver.got[0][1]["iv"], Receiver.got[1][1]["iv"])
        log = webhook.deliveries("wh_t")
        self.assertEqual([(d["attempt"], d["ok"]) for d in log], [(2, True), (1, False)])

    def test_gives_up_after_retries(self):
        Receiver.codes = [500, 500]
        logs = []
        self.assertFalse(webhook._deliver({**self.hook, "id": "wh_f"}, "message", "evt_y", {}, logs.append,
                                          sleep=lambda s: None, retry_delays=(0, 1)))
        self.assertEqual(webhook._stats["wh_f"]["fail"], 1)
        self.assertTrue(logs and "投递失败" in logs[0])

    def test_dispatch_does_not_block(self):
        slow = {**self.hook, "id": "wh_s", "url": "http://10.255.255.1:9/x", "enabled": True,
                "all_chats": True, "groups": True, "private": True}
        t = time.time()
        with patch.object(webhook, "_deliver") as d:
            n = webhook.dispatch("wxid_a", {"local_id": 1}, {"message": {}}, "me", hooks=[slow])
        self.assertEqual(n, 1)
        self.assertLess(time.time() - t, 0.5)

    def test_oversized_content_truncated(self):
        big = {"event": "message", "message": {"content": "字" * 200000, "meta": {"x": 1}}}
        fit = webhook._fit(big)
        self.assertTrue(fit["message"]["truncated"])
        self.assertLessEqual(len(json.dumps(fit, ensure_ascii=False).encode()), webhook.MAX_BODY)


if __name__ == "__main__":
    unittest.main()
