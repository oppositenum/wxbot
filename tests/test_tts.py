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
        st.assert_called_once_with("wxid_a", "hi", None, "", source="manual")


if __name__ == "__main__":
    unittest.main()
