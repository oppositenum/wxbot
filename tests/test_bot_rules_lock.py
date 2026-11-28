import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


class RulesWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"WXBOT_UI_AUTH": "1", "WXBOT_UI_USER": "u", "WXBOT_UI_PASSWORD": "p",
                                           "WXBOT_SECRET": "s"})
        self.env.start()
        self.p = patch("config.account_dir", return_value=self.tmp.name)
        self.p.start()
        from core import bot
        self.bot = bot
        bot.save_rules({"watch": [], "rules": [], "padding": "x" * 200000})

    def tearDown(self):
        self.p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_concurrent_saves_never_leave_broken_json(self):
        stop = threading.Event()
        bad = []

        def reader():
            while not stop.is_set():
                try:
                    with open(self.bot.rules_file(), encoding="utf-8") as f:
                        json.load(f)
                except ValueError as e:
                    bad.append(str(e))

        def writer(i):
            for _ in range(20):
                r = self.bot.load_rules()
                r["n"] = i
                self.bot.save_rules(r)
        t = threading.Thread(target=reader)
        t.start()
        ws = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
        [w.start() for w in ws]
        [w.join() for w in ws]
        stop.set()
        t.join()
        self.assertEqual(bad, [])
        self.assertEqual([n for n in os.listdir(self.tmp.name) if ".tmp" in n], [])

    def test_parallel_setting_endpoints_keep_both_changes(self):
        import server
        c1, c2 = server.app.test_client(), server.app.test_client()
        for c in (c1, c2):
            with c.session_transaction() as s:
                s["ui_auth"] = True
                s["ui_seen"] = time.time()
        orig = self.bot.load_rules

        def slow_load():
            r = orig()
            time.sleep(0.05)          # 放大「读-改-写」窗口
            return r
        with patch.object(self.bot, "load_rules", side_effect=slow_load):
            a = threading.Thread(target=lambda: c1.post("/api/bot/watch", json={"watch": ["wxid_a"]}))
            b = threading.Thread(target=lambda: c2.post("/api/bot/proactive", json={"enabled": True}))
            a.start(); b.start(); a.join(); b.join()
        r = self.bot.load_rules()
        self.assertEqual(r["watch"], ["wxid_a"])
        self.assertTrue(r["proactive"]["enabled"])


if __name__ == "__main__":
    unittest.main()
