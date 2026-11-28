import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from core import tts


class TTSConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = patch("config.WORK_DIR", self.tmp.name)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_presets_and_default(self):
        cfg = tts.get_config()
        self.assertEqual(cfg["default"], tts.DEFAULT_KEY)
        self.assertEqual({v["key"] for v in cfg["voices"]}, set(tts.PRESETS))
        self.assertEqual(tts.get_voice()["key"], tts.DEFAULT_KEY)

    def test_override_builtin_only_stores_diff_and_restores(self):
        tts.save_voice("calm_deep", {"rate": "-20%", "pitch": tts.PRESETS["calm_deep"]["pitch"]})
        v = tts.get_voice("calm_deep")
        self.assertEqual((v["rate"], v["changed"]), ("-20%", True))
        self.assertEqual(tts._read()["voices"]["calm_deep"], {"rate": "-20%"})
        tts.delete_voice("calm_deep")
        self.assertEqual(tts.get_voice("calm_deep")["rate"], tts.PRESETS["calm_deep"]["rate"])

    def test_custom_voice_and_default(self):
        with self.assertRaises(tts.TTSError):
            tts.save_voice("mine", {"rate": "-5%"})            # 缺显示名和音色名
        tts.save_voice("mine", {"label": "我的", "name": "zh-TW-HsiaoChenNeural", "pitch": "-2Hz"})
        tts.set_default("mine")
        self.assertEqual(tts.get_voice()["name"], "zh-TW-HsiaoChenNeural")
        tts.delete_voice("mine")
        self.assertEqual(tts.get_config()["default"], tts.DEFAULT_KEY)

    def test_keep_only_hides_presets_but_still_resolves_them(self):
        tts.add_clone_voice("my_clone", "我的声音", "aliyun", "cosyvoice-v3.5-plus", "/tmp/none.wav")
        cfg = tts.keep_only(["my_clone"])
        self.assertEqual([v["key"] for v in cfg["voices"]], ["my_clone"])
        self.assertEqual(cfg["default"], "my_clone")
        self.assertEqual(set(cfg["hidden"]), set(tts.PRESETS))
        self.assertEqual(tts.get_voice()["key"], "my_clone")
        self.assertEqual(tts.get_voice(tts.DEFAULT_KEY)["key"], tts.DEFAULT_KEY)   # 兜底仍可用
        self.assertEqual(len(tts.get_config(include_hidden=True)["voices"]), len(tts.PRESETS) + 1)
        cfg = tts.set_hidden([])
        self.assertEqual(len(cfg["voices"]), len(tts.PRESETS) + 1)
        self.assertEqual(cfg["default"], "my_clone")

    def test_hidden_default_falls_back_to_visible(self):
        tts.set_hidden(["warm_low", "nope"])
        cfg = tts.get_config()
        self.assertEqual(cfg["hidden"], ["warm_low"])
        self.assertNotEqual(cfg["default"], "warm_low")
        self.assertIn(cfg["default"], {v["key"] for v in cfg["voices"]})

    def test_validation(self):
        for bad in ({"rate": "fast"}, {"pitch": "-5%"}, {"name": "rm -rf /"}):
            with self.assertRaises(tts.TTSError):
                tts.save_voice("warm_low", bad)
        with self.assertRaises(tts.TTSError):
            tts.save_voice("Bad Key", {"label": "x", "name": "zh-CN-YunxiNeural"})
        with self.assertRaises(tts.TTSError):
            tts.get_voice("nope")

    def test_synth_uses_voice_params(self):
        calls = {}

        class Communicate:
            def __init__(self, text, name, rate, pitch, volume):
                calls.update(text=text, name=name, rate=rate, pitch=pitch, volume=volume)

            async def save(self, path):
                with open(path, "wb") as f:
                    f.write(b"ID3fake")

        fake = types.SimpleNamespace(Communicate=Communicate)
        with patch.dict(sys.modules, {"edge_tts": fake}):
            path = tts.synth("  你好\n世界 ", "magnetic")
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(calls, {"text": "你好 世界", "name": "zh-CN-YunjianNeural", "rate": "-10%",
                                 "pitch": "-5Hz", "volume": "+0%"})
        with self.assertRaises(tts.TTSError):
            tts.synth("")
        with self.assertRaises(tts.TTSError):
            tts.synth("字" * (tts.MAX_TEXT + 1))


class TTSRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch("config.WORK_DIR", self.tmp.name),
                        patch.dict(os.environ, {"WXBOT_UI_AUTH": "1", "WXBOT_UI_USER": "u", "WXBOT_UI_PASSWORD": "p",
                                                "WXBOT_SECRET": "s", "WXBOT_API_TOKEN": "api-token"}, clear=False),
                        patch("core.tts.list_edge_voices", return_value=[{"name": "zh-CN-YunxiNeural"}])]
        for p in self.patches:
            p.start()
        import server
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_config_needs_login_then_saves(self):
        self.assertEqual(self.client.get("/api/tts/config").status_code, 401)
        self.client.post("/api/auth/login", json={"username": "u", "password": "p"})
        d = self.client.get("/api/tts/config").get_json()
        self.assertEqual(d["edge_voices"][0]["name"], "zh-CN-YunxiNeural")
        r = self.client.post("/api/tts/config", json={"voice": {"key": "magnetic", "rate": "-30%"}, "default": "magnetic"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(tts.get_voice()["rate"], "-30%")
        self.assertEqual(self.client.post("/api/tts/config", json={"voice": {"key": "x", "rate": "fast"}}).status_code, 400)

    def test_preview_returns_mp3_and_send_tts_accepts_token(self):
        self.client.post("/api/auth/login", json={"username": "u", "password": "p"})
        out = os.path.join(self.tmp.name, "a.mp3")
        with open(out, "wb") as f:
            f.write(b"ID3x")
        with patch("core.tts.synth", return_value=out):
            r = self.client.post("/api/tts/preview", json={"text": "hi", "voice": "sunny"})
        self.assertEqual((r.status_code, r.mimetype, r.data), (200, "audio/mpeg", b"ID3x"))
        self.client.post("/api/auth/logout")
        with patch("core.direct_send.send_tts", return_value={"ok": True}) as st:
            r = self.client.post("/api/direct/send_tts", json={"token": "api-token", "chat": "wxid_a", "text": "hi"})
        self.assertEqual(r.status_code, 200)
        st.assert_called_once_with("wxid_a", "hi", None, "", source="manual", send_as=None)


if __name__ == "__main__":
    unittest.main()


class SendAsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = patch("config.WORK_DIR", self.tmp.name)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_send_as_config(self):
        self.assertEqual(tts.send_as(), "video")
        tts.set_send_as("file")
        self.assertEqual(tts.get_config()["send_as"], "file")
        with self.assertRaises(tts.TTSError):
            tts.set_send_as("voice_message")

    def test_send_tts_file_mode_uses_readable_name(self):
        from core import direct_send
        mp3 = os.path.join(self.tmp.name, "x.mp3")
        with open(mp3, "wb") as f:
            f.write(b"ID3data")
        tts.set_send_as("file")
        with patch("core.tts.synth", return_value=mp3), \
                patch("core.direct_send.FILE_DIR", os.path.join(self.tmp.name, "files")), \
                patch("core.direct_send.send_file", side_effect=lambda c, p, *a, **k: {"path": p}) as sf, \
                patch("core.direct_send.send_voice") as sv:
            r = direct_send.send_tts("wxid_a", "晚上好。早点休息！")
        sv.assert_not_called()
        self.assertEqual(os.path.basename(r["path"]), "语音_晚上好早点休息.mp3")
        with patch("core.tts.synth", return_value=mp3), patch("core.direct_send.send_voice", return_value={"ok": 1}) as sv:
            direct_send.send_tts("wxid_a", "hi", send_as="video")
        sv.assert_called_once()
        self.assertEqual(sf.call_count, 1)

    def test_stage_file_keeps_name(self):
        from core import direct_send
        with patch("core.direct_send.FILE_DIR", self.tmp.name):
            p = direct_send.stage_file(b"x", "../../etc/晚安.mp3")
        self.assertEqual(os.path.basename(p), "晚安.mp3")
        self.assertTrue(os.path.realpath(p).startswith(os.path.realpath(self.tmp.name)))


class VoiceBarModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = patch("config.WORK_DIR", self.tmp.name)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_voice_bar_mode_routes_to_voice_bar(self):
        from core import direct_send
        tts.set_send_as("voice_bar")
        with patch("core.voice_bar.send_tts", return_value={"status": "submitted", "seconds": 3.1}) as vb, \
                patch("core.direct_send._record"):
            row = direct_send.send_tts("wxid_a", "晚上好", "calm_deep", "小A")
        vb.assert_called_once_with("wxid_a", "晚上好", "calm_deep", "小A")
        self.assertTrue(row["ok"])
        self.assertEqual(row["kind"], "voice_bar")

    def test_voice_bar_not_sent_raises(self):
        from core import direct_send
        with patch("core.voice_bar.send_tts", return_value={"status": "not_sent", "reason": "legacy_voice_ui"}), \
                patch("core.direct_send._record"):
            with self.assertRaises(direct_send.DirectSendError) as cm:
                direct_send.send_tts("wxid_a", "hi", send_as="voice_bar")
        self.assertEqual(cm.exception.code, "legacy_voice_ui")
