"""AI 语音通话:名字核对、名单、设置、定时电话(不真的打电话)。"""
import tempfile
import unittest
from unittest.mock import patch

from core import schedule, voice_call


class NameMatch(unittest.TestCase):
    def test_besos_variants_are_told_apart(self):
        self.assertTrue(voice_call.name_matches("🐮🐮🌱besos", "击besos"))        # 表情被认成一个怪字
        self.assertTrue(voice_call.name_matches("🐮🐮🌱besos", "高谢咱besos"))    # 3 个表情 → 最多 3 个怪字
        others = ["蜜蜜🌱besos"]
        self.assertFalse(voice_call.name_matches("🐮🐮🌱besos", "蜜蜜咱besos", others=others))  # 相似联系人的独有字
        self.assertTrue(voice_call.name_matches("🐮🐮🌱besos", "高谢咱besos", others=others))
        self.assertFalse(voice_call.name_matches("夏以昼", "高谢夏以昼"))            # 没表情:只容 1 个多余字
        self.assertFalse(voice_call.name_matches("🐮🐮🌱besos", "蜜蜜besos", others=others))   # 靠相似联系人排除
        self.assertFalse(voice_call.name_matches("蜜蜜🌱besos", "击besos"))
        self.assertTrue(voice_call.name_matches("蜜蜜🌱besos", "蜜蜜besos"))
        self.assertTrue(voice_call.name_matches("夏以昼", "夏以昼"))
        self.assertFalse(voice_call.name_matches("夏以昼", "夏以"))
        self.assertFalse(voice_call.name_matches("", "anything"))
        self.assertFalse(voice_call.name_matches("夏以昼", "夏以尽"))                  # 拨号核对:严格
        self.assertTrue(voice_call.name_matches("夏以昼", "夏以尽", fuzzy=True))       # 列表挑候选:容 1 个错字
        self.assertFalse(voice_call.name_matches("夏以昼", "夏一尽", fuzzy=True))
        self.assertFalse(voice_call.name_matches("老公", "老婆", fuzzy=True))          # 两个字的名字不容错

    def test_fragments(self):
        for t, ok in (("Yeah. ", False), ("个。", False), ("嗯。", True), ("好", True),
                      ("我把鸡蛋弄碎。", True), ("I am fine thanks", True)):
            self.assertEqual(voice_call.meaningful(t), ok, t)


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = patch.object(voice_call.config, "account_dir", return_value=self.tmp.name)
        p.start(); self.addCleanup(p.stop); self.addCleanup(self.tmp.cleanup)

    def test_allow_list_defaults_to_test_account_only(self):
        self.assertTrue(voice_call.allowed("wxid_vn2w1t3doieu22"))
        self.assertFalse(voice_call.allowed("wxid_other"))
        s = voice_call.save_settings({"allow": [{"username": "wxid_a", "name": "A"}, "wxid_a", "1@chatroom"],
                                      "max_minutes": 99})
        self.assertEqual([x["username"] for x in s["allow"]], ["wxid_a"])          # 去重、不收群
        self.assertEqual(s["max_minutes"], 30)
        self.assertTrue(voice_call.allowed("wxid_a"))
        self.assertFalse(voice_call.allowed("wxid_vn2w1t3doieu22"))                 # 配了名单就只认名单

    def test_place_call_refuses_outside_list_and_when_busy(self):
        voice_call.save_settings({"allow": ["wxid_a"]})
        with self.assertRaises(voice_call.CallError):
            voice_call.place_call("wxid_b")
        self.assertTrue(voice_call._lock.acquire(blocking=False))
        try:
            with self.assertRaises(voice_call.CallError):
                voice_call.place_call("wxid_a")
        finally:
            voice_call._lock.release()


class ScheduledCall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, kw in ((voice_call.config, {"account_dir": self.tmp.name}),):
            for name, val in kw.items():
                p = patch.object(target, name, return_value=val)
                p.start(); self.addCleanup(p.stop)
        self.saved = []
        for name, val in (("load_tasks", []),):
            p = patch.object(schedule, name, return_value=list(val))
            p.start(); self.addCleanup(p.stop)
        p = patch.object(schedule, "save_tasks", side_effect=self.saved.append)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(schedule, "resolve_target",
                         side_effect=lambda t: ("1@chatroom", True, "群") if t == "群" else ("wxid_" + t, False, t))
        p.start(); self.addCleanup(p.stop)

    def test_call_task_needs_allow_list_or_own_request(self):
        voice_call.save_settings({"allow": ["wxid_mom"]})
        t, err = schedule.add_task("吃药", "mom", "提醒吃药", once_at="2026-12-01 08:00", kind="call")
        self.assertIsNone(err); self.assertEqual(t["kind"], "call")
        t, err = schedule.add_task("吃药", "bob", "提醒吃药", once_at="2026-12-01 08:00", kind="call")
        self.assertIn("可拨打名单", err)
        t, err = schedule.add_task("x", "群", "x", once_at="2026-12-01 08:00", kind="call")
        self.assertIn("不能打给群", err)
        # 本人在聊天里要求「给我打电话」:要开 self_request 才行
        args = dict(once_at="2026-12-01 08:00", kind="call", self_request=True, creator_wxid="wxid_bob")
        self.assertIsNotNone(schedule.add_task("吃药", "bob", "提醒吃药", **args)[1])
        voice_call.save_settings({"self_request": True})
        self.assertIsNone(schedule.add_task("吃药", "bob", "提醒吃药", **args)[1])
        self.assertIsNotNone(schedule.add_task("吃药", "carol", "提醒吃药", **args)[1])   # 不能替别人要

    def test_nl_call_creates_call_task(self):
        voice_call.save_settings({"allow": ["wxid_蜜蜜"]})
        parsed = {"action": "create", "title": "提醒吃药", "target": "我", "once_at": "2026-12-01 08:00",
                  "prompt": "提醒吃药", "call": True, "cron": "", "crons": []}
        with patch.object(schedule, "parse_nl", return_value=parsed), \
             patch.object(schedule.llm, "available", return_value=True), \
             patch.object(schedule, "recent_fires", return_value=[]), \
             patch.object(schedule.sessions, "check"):
            r = schedule.handle_nl.__wrapped__("明天早上8点给我打电话提醒我吃药",
                                               {"chat_username": "wxid_蜜蜜", "chat_display": "蜜蜜",
                                                "is_group": False, "requester_wxid": "wxid_蜜蜜"}) \
                if hasattr(schedule.handle_nl, "__wrapped__") else None
        if r is None:
            self.skipTest("handle_nl 没有 __wrapped__")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["task"]["kind"], "call")
        self.assertIn("打电话", r["message"])

    def test_fire_falls_back_to_text_when_busy(self):
        sent = []

        class Ledger:
            def update(self, jid, status, reason):
                return {"status": status, "reason": reason}

            def view(self, row):
                return row
        with patch.object(voice_call, "place_call", side_effect=voice_call.CallError("正在通话中")), \
             patch("core.sender.send_text", side_effect=lambda *a, **k: sent.append((a, k)) or
                   {"status": "submitted", "reason": "ok"}), \
             patch.object(schedule, "_record_fire"):
            r = schedule._fire_call({"target_username": "wxid_mom", "target_display": "妈", "prompt": "提醒吃药"},
                                    "jid1", Ledger(), {"account": "me"})
        self.assertEqual(r["status"], "submitted")
        self.assertIn("提醒吃药", sent[0][0][1])
        self.assertTrue(sent[0][0][1].startswith(schedule.PREFIX))


if __name__ == "__main__":
    unittest.main()
