import tempfile
import unittest
from unittest.mock import patch

from core import voice_reply as vr


class VoiceReplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = [patch("config.WORK_DIR", self.tmp.name), patch("core.voice_reply.available", return_value=True)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.tmp.cleanup()

    def test_off_by_default(self):
        self.assertEqual(vr.decide("c", "发条语音", "好呀"), (False, "disabled"))
        self.assertEqual(vr.prompt("c"), "")

    def test_request_mode_only_on_ask(self):
        vr.set_global(enabled=True, mode="request")
        self.assertEqual(vr.decide("c", "你好", "[[VOICE]]晚安")[0], False)
        self.assertEqual(vr.decide("c", "发条语音给我", "晚安"), (True, "asked_for_voice"))
        self.assertEqual(vr.decide("c", "想听你的声音", "好"), (True, "asked_for_voice"))
        self.assertEqual(vr.decide("c", "别发语音了，打字吧", "好"), (False, "asked_for_text"))
        self.assertEqual(vr.prompt("c"), "")

    def test_auto_mode_uses_mark_and_chance(self):
        vr.set_global(enabled=True, mode="auto", chance=0.5)
        self.assertIn("[[VOICE]]", vr.prompt("c"))
        self.assertEqual(vr.decide("c", "晚安", "[[VOICE]]晚安呀", rnd=lambda: 0.1), (True, "model_marked"))
        self.assertEqual(vr.decide("c", "晚安", "[[VOICE]]晚安呀", rnd=lambda: 0.9), (False, "model_marked"))
        self.assertEqual(vr.decide("c", "晚安", "晚安呀")[0], False)

    def test_explicit_ask_tells_model_and_allows_longer(self):
        vr.set_global(enabled=True, mode="request", max_chars=60)
        p = vr.prompt("c", "用语音发给我")
        self.assertIn("语音条发出", p)
        self.assertIn("150", p)
        self.assertEqual(vr.prompt("c", "用语音发给我，算了别发语音了打字吧"), "")
        self.assertEqual(vr.decide("c", "用语音发给我", "好" * 95), (True, "asked_for_voice"))
        self.assertEqual(vr.decide("c", "用语音发给我", "好" * 200), (False, "too_long"))
        vr.set_global(mode="always")
        self.assertIn("60 字以内", vr.prompt("c", "你好"))

    def test_per_contact_voice_used_for_manual_tts(self):
        from core import direct_send
        vr.set_chat("wxid_a", voice="")          # 先建会话覆盖再设音色(set_chat 会校验音色存在)
        with patch("core.tts.get_voice", return_value={"key": "my", "label": "我"}):
            vr.set_chat("wxid_a", voice="my")
        self.assertEqual(vr.voice_for("wxid_a"), "my")
        self.assertIsNone(vr.voice_for("wxid_b"))
        seen = []
        with patch.object(direct_send, "_send_voice_bar", side_effect=lambda c, t, v, n, s: seen.append(v)), \
             patch.object(direct_send, "_check_chat", side_effect=lambda c: c):
            direct_send.send_tts("wxid_a", "你好", send_as="voice_bar")
            direct_send.send_tts("wxid_b", "你好", send_as="voice_bar")
        self.assertEqual(seen, ["my", None])          # 没单独设置的好友用默认音色

    def test_too_long_stays_text(self):
        vr.set_global(enabled=True, mode="always", max_chars=10)
        self.assertEqual(vr.decide("c", "你好", "这句话肯定超过了十个字的长度啊")[1], "too_long")
        self.assertTrue(vr.decide("c", "", "好的")[0])

    def test_chat_override_and_voice(self):
        vr.set_global(enabled=False, mode="auto")
        vr.set_chat("c1", enabled=True, mode="always", voice="calm_deep")
        self.assertTrue(vr.decide("c1", "", "好")[0])
        self.assertFalse(vr.decide("c2", "", "好")[0])
        self.assertEqual(vr.voice_for("c1"), "calm_deep")
        self.assertIsNone(vr.voice_for("c2"))
        vr.clear_chat("c1")
        self.assertFalse(vr.decide("c1", "", "好")[0])

    def test_remembers_preference(self):
        vr.set_global(enabled=True, mode="request")
        self.assertEqual(vr.remember_preference("c", "以后都用语音回我吧"), "always")
        self.assertTrue(vr.decide("c", "嗯", "好")[0])
        self.assertEqual(vr.remember_preference("c", "以后别发语音了"), "off")
        self.assertFalse(vr.decide("c", "嗯", "好")[0])
        self.assertIsNone(vr.remember_preference("c", "今天吃什么"))

    def test_spoken_text_is_clean(self):
        self.assertEqual(vr.spoken("[[VOICE]]晚安呀[旺柴]😊[[NEXT]]早点睡"), "晚安呀，早点睡")

    def test_validation(self):
        with self.assertRaises(ValueError):
            vr.set_global(mode="sometimes")
        with self.assertRaises(ValueError):
            vr.set_global(chance=2)
        from core import tts
        with self.assertRaises(tts.TTSError):
            vr.set_global(voice="no_such_voice")

    def test_unavailable_wechat(self):
        vr.set_global(enabled=True, mode="always")
        with patch("core.voice_reply.available", return_value=False):
            self.assertEqual(vr.decide("c", "发语音", "好"), (False, "disabled"))


if __name__ == "__main__":
    unittest.main()
