import base64
import os
import shutil
import subprocess
import tempfile
import unittest
import wave
from unittest.mock import patch

from core import voice_clone, tts


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = patch("config.WORK_DIR", self.tmp.name)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()


@unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
class PrepareSampleTest(Base):
    def _speech_with_gaps(self, n=12, talk=2.0, gap=3.0):
        """模拟说话:2 秒声音 + 3 秒静音,重复 n 次。"""
        src = os.path.join(self.tmp.name, "src.wav")
        expr = f"if(lt(mod(t,{talk + gap}),{talk}),0.5*sin(2*PI*220*t),0)"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"aevalsrc='{expr}':s=24000:d={n * (talk + gap)}", src], check=True)
        return src

    def test_long_pauses_are_squeezed_and_length_capped(self):
        sample, secs = voice_clone.prepare_sample(self._speech_with_gaps())
        self.assertTrue(10 <= secs <= 22, secs)
        with wave.open(sample, "rb") as w:
            self.assertEqual((w.getnchannels(), w.getframerate()), (1, 24000))
        out = subprocess.run(["ffmpeg", "-hide_banner", "-i", sample, "-af", "silencedetect=noise=-35dB:d=1.5",
                              "-f", "null", "-"], capture_output=True, text=True).stderr
        self.assertNotIn("silence_start", out)          # 没有超过 1.5 秒的停顿

    def test_too_little_speech(self):
        with self.assertRaises(voice_clone.CloneError) as cm:
            voice_clone.prepare_sample(self._speech_with_gaps(n=2))
        self.assertEqual(cm.exception.code, "too_short")


class EnrollTest(Base):
    def test_sends_base64_data_uri_and_registers_voice(self):
        wav = os.path.join(self.tmp.name, "s.wav")
        with open(wav, "wb") as f:
            f.write(b"RIFFfake")
        seen = {}

        def fake_call(inp, timeout=90):
            seen.update(inp)
            return {"output": {"voice_id": "cosyvoice-v3.5-plus-x-1"}}

        with patch("core.voice_clone._call", side_effect=fake_call), \
                patch("core.voice_clone.prepare_sample", return_value=(wav, 15.0)):
            key, vid, _ = voice_clone.create_voice("/any.mp4", "我的声音", key="my_clone")
        self.assertEqual(seen["action"], "create_voice")
        self.assertEqual(seen["target_model"], voice_clone.MODEL)
        self.assertEqual(seen["url"], "data:audio/wav;base64," + base64.b64encode(b"RIFFfake").decode())
        v = tts.get_voice("my_clone")
        self.assertEqual((v["provider"], v["voice_id"], v["label"]), ("cosyvoice", vid, "我的声音"))

    def test_key_from_file_when_env_missing(self):
        kf = os.path.join(self.tmp.name, "k")
        with open(kf, "w") as f:
            f.write("sk-test\n")
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""}), patch("core.voice_clone.KEY_FILE", kf):
            self.assertEqual(voice_clone.api_key(), "sk-test")
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""}), patch("core.voice_clone.KEY_FILE", "/nope"):
            with self.assertRaises(voice_clone.CloneError):
                voice_clone.api_key()


class SynthRoutingTest(Base):
    def test_clone_voice_synth_and_fallback(self):
        tts.add_clone_voice("my_clone", "我的声音", "vid-1", "cosyvoice-v3.5-plus")
        with patch("core.voice_clone.synth", side_effect=lambda text, vid, model, path: path) as cs:
            p = tts.synth("你好", "my_clone")
        self.assertTrue(p.endswith(".clone.mp3"))
        self.assertEqual(cs.call_args[0][1:3], ("vid-1", "cosyvoice-v3.5-plus"))

        calls = {}

        class Communicate:
            def __init__(self, text, name, rate, pitch, volume):
                calls["name"] = name

            async def save(self, path):
                open(path, "wb").write(b"ID3")

        import sys, types
        with patch("core.voice_clone.synth", side_effect=voice_clone.CloneError("synth_failed", "额度用完")), \
                patch.dict(sys.modules, {"edge_tts": types.SimpleNamespace(Communicate=Communicate)}):
            p = tts.synth("你好", "my_clone")
        self.assertTrue(p.endswith(".tts.mp3"))
        self.assertEqual(calls["name"], tts.PRESETS[tts.DEFAULT_KEY]["name"])

    def test_clone_voice_only_label_editable_and_delete_removes_remote(self):
        tts.add_clone_voice("my_clone", "旧名", "vid-1", "m")
        tts.save_voice("my_clone", {"label": "新名", "rate": "-20%"})
        v = tts.get_voice("my_clone")
        self.assertEqual((v["label"], v["rate"]), ("新名", "+0%"))
        with patch("core.voice_clone.delete_voice") as dv:
            tts.delete_voice("my_clone")
        dv.assert_called_once_with("vid-1")
        with self.assertRaises(tts.TTSError):
            tts.get_voice("my_clone")


if __name__ == "__main__":
    unittest.main()
