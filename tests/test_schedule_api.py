import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from core import schedule


class ScheduleApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"WXBOT_UI_AUTH": "1", "WXBOT_UI_USER": "u", "WXBOT_UI_PASSWORD": "p",
                                           "WXBOT_SECRET": "s"})
        self.env.start()
        acct = os.path.join(self.tmp.name, "offline-test")
        os.makedirs(acct)
        self.p = [patch("config.ACCOUNTS_DIR", self.tmp.name), patch("config.wxid", return_value="offline-test"),
                  patch("config.account_dir", return_value=acct),
                  patch.object(schedule, "resolve_target",
                               side_effect=lambda n: ("g1@chatroom", True, "测试群") if n else (None, None, None)),
                  patch.object(schedule, "resolve_member", return_value=("wxid_m", "小明"))]
        for x in self.p:
            x.start()
        import server
        self.c = server.app.test_client()
        with self.c.session_transaction() as s:
            s["ui_auth"] = True
            s["ui_seen"] = time.time()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_create_disabled_task_from_form(self):
        r = self.c.post("/api/schedule", json={"target": "g1@chatroom", "title": "开会", "prompt": "记得开会",
                                               "cron": "0 9 * * 1", "mention": "小明", "persona": "P1",
                                               "enabled": False})
        d = r.get_json()
        self.assertTrue(d["ok"], d)
        t = schedule.load_tasks()[0]
        self.assertEqual((t["cron"], t["mention_wxid"], t["persona"], t["enabled"]), ("0 9 * * 1", "wxid_m", "P1", False))
        bad = self.c.post("/api/schedule", json={"target": "g1@chatroom", "prompt": "x", "cron": "99 * * * *"})
        self.assertEqual(bad.status_code, 400)
        none = self.c.post("/api/schedule", json={"target": "g1@chatroom", "prompt": "x"})
        self.assertEqual(none.status_code, 400)

    def test_clear_persona(self):
        t, _ = schedule.add_task("t", "g1@chatroom", "x", cron="0 9 * * *", persona="P1")
        t, err = schedule.update_task(t["id"], {"persona": ""})
        self.assertIsNone(err)
        self.assertIsNone(t["persona"])

    def test_fires_filtered_by_task(self):
        json.dump([{"ts": 1, "task_id": 1, "chat": "a", "text": "x", "ok": True},
                   {"ts": 2, "task_id": 2, "chat": "b", "text": "y", "ok": True}],
                  open(os.path.join(self.tmp.name, "offline-test", "schedule_fires.json"), "w"))
        schedule._fires_cache["mtime"] = None
        d = self.c.get("/api/schedule/fires?task_id=2").get_json()
        self.assertEqual([f["text"] for f in d["fires"]], ["y"])
        d = self.c.get("/api/schedule/fires").get_json()
        self.assertEqual([f["text"] for f in d["fires"]], ["y", "x"])


if __name__ == "__main__":
    unittest.main()
