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


class FakeProvider:
    """记录调用的假厂商:enroll 返回 vid-<key>-<n>,synth 返回 b"MP3"。"""
    id = "fake"
    label = "假厂商"
    models = [("m1", "m1"), ("m2", "m2")]
    default_model = "m1"
    model_bound = True
    auto_enroll_default = True

    def __init__(self):
        self.enrolled, self.synths, self.deleted = [], [], []
        self.synth_error = None

    def enroll(self, key, sample, name, model, opts=None):
        self.enrolled.append((key, model))
        return f"vid-{key}-{len(self.enrolled)}"

    def synth(self, key, text, voice_id, model):
        self.synths.append((key, voice_id, model))
        if self.synth_error:
            e, self.synth_error = self.synth_error, None
            raise e
        return b"MP3"

    def delete(self, key, voice_id):
        self.deleted.append((key, voice_id))


class CloneFlowTest(Base):
    def setUp(self):
        super().setUp()
        from core import voice_providers
        self.fp = FakeProvider()
        self.pp = patch.dict(voice_providers.PROVIDERS, {"fake": self.fp})
        self.kp = patch.dict(os.environ, {"WXBOT_VOICE_KEYS": os.path.join(self.tmp.name, "keys.json"),
                                          "DASHSCOPE_API_KEY": ""})
        self.pp.start(); self.kp.start()
        self.lk = patch("core.voice_clone.KEY_FILE", "/nope"); self.lk.start()
        from core import voice_keys
        self.vk = voice_keys
        self.wav = os.path.join(self.tmp.name, "s.wav"); open(self.wav, "wb").write(b"RIFF")

    def tearDown(self):
        self.pp.stop(); self.kp.stop(); self.lk.stop()
        super().tearDown()

    def create(self, **kw):
        with patch("core.voice_clone.prepare_sample", return_value=(self.wav, 15.0)):
            return voice_clone.create_voice("/any.mp4", "我的声音", key="my_clone", provider="fake", **kw)

    def test_create_enrolls_with_first_key_and_keeps_sample(self):
        self.vk.add("fake", "KEY-A", group="acct1")
        key, vid, _ = self.create()
        v = tts.get_voice("my_clone")
        self.assertEqual((key, vid), ("my_clone", "vid-KEY-A-1"))
        self.assertEqual(v["ids"], {"acct1|m1": "vid-KEY-A-1"})
        self.assertTrue(os.path.isfile(v["sample"]) and "samples" in v["sample"])

    def test_create_without_key_leaves_nothing(self):
        with self.assertRaises(voice_clone.CloneError):
            self.create()
        with self.assertRaises(tts.TTSError):
            tts.get_voice("my_clone")

    def test_rotates_on_exhausted_and_enrolls_in_new_group(self):
        from core.voice_providers import ProviderError
        self.vk.add("fake", "KEY-A", group="acct1")
        self.create()
        self.vk.add("fake", "KEY-B", group="acct2")
        # 不管轮询从哪个 key 开始:第一次合成报余额不足,应换到另一个 key,并在缺音色的账号下补复刻
        self.fp.synth_error = ProviderError("exhausted", "余额不足")
        p = tts.synth("你好", "my_clone")
        self.assertTrue(p.endswith(".clone.mp3"))
        ids = tts.get_voice("my_clone")["ids"]
        self.assertEqual(set(ids), {"acct1|m1", "acct2|m1"})
        st = {k["id"]: k for k in self.vk.view("fake")["fake"]["keys"]}
        bad = [k for k in st.values() if k["status"] == "exhausted"]
        self.assertEqual(len(bad), 1)
        self.assertNotEqual(self.fp.synths[-1][0], self.fp.synths[0][0])

    def test_no_auto_enroll_skips_foreign_group(self):
        self.vk.add("fake", "KEY-A", group="acct1")
        self.create()
        self.vk.add("fake", "KEY-B", group="acct2")
        self.vk.set_auto_enroll("fake", False)
        for _ in range(3):
            tts.synth("你好", "my_clone")
        self.assertEqual({k for k, _vid, _m in self.fp.synths}, {"KEY-A"})
        self.assertEqual(len(self.fp.enrolled), 1)

    def test_voice_missing_reenrolls_once(self):
        from core.voice_providers import ProviderError
        self.vk.add("fake", "KEY-A")
        self.create()
        self.fp.synth_error = ProviderError("voice_missing", "voice not exist")
        tts.synth("你好", "my_clone")
        self.assertEqual(len(self.fp.enrolled), 2)
        self.assertEqual(self.fp.synths[-1][1], "vid-KEY-A-2")

    def test_model_change_on_bound_provider_enrolls_again(self):
        self.vk.add("fake", "KEY-A", group="g")
        self.create()
        tts.save_voice("my_clone", {"model": "m2", "rate": "-20%"})
        v = tts.get_voice("my_clone")
        self.assertEqual((v["model"], v["rate"]), ("m2", "+0%"))
        tts.synth("你好", "my_clone")
        self.assertEqual(self.fp.enrolled[-1], ("KEY-A", "m2"))
        with self.assertRaises(tts.TTSError):
            tts.save_voice("my_clone", {"model": "nope"})

    def test_all_keys_dead_falls_back_to_edge(self):
        from core.voice_providers import ProviderError
        self.vk.add("fake", "KEY-A")
        self.create()
        self.fp.synth_error = ProviderError("auth", "invalid key")
        calls = {}

        class Communicate:
            def __init__(self, text, name, rate, pitch, volume):
                calls["name"] = name

            async def save(self, path):
                open(path, "wb").write(b"ID3")

        import sys, types
        with patch.dict(sys.modules, {"edge_tts": types.SimpleNamespace(Communicate=Communicate)}):
            p = tts.synth("你好", "my_clone")
        self.assertTrue(p.endswith(".tts.mp3"))
        self.assertEqual(calls["name"], tts.PRESETS[tts.DEFAULT_KEY]["name"])
        self.assertEqual(self.vk.view("fake")["fake"]["keys"][0]["status"], "auth")

    def test_clone_from_existing_voice_reuses_sample(self):
        self.vk.add("fake", "KEY-A")
        self.create()
        k2, _vid, _ = voice_clone.create_voice(None, "对比", key="cmp", provider="fake", model="m2",
                                               from_voice="my_clone")
        self.assertEqual(open(tts.get_voice(k2)["sample"], "rb").read(), b"RIFF")
        self.assertEqual(self.fp.enrolled[-1][1], "m2")

    def test_delete_removes_remote_per_group(self):
        self.vk.add("fake", "KEY-A", group="g")
        self.create()
        tts.delete_voice("my_clone")
        self.assertEqual(self.fp.deleted, [("KEY-A", "vid-KEY-A-1")])
        with self.assertRaises(tts.TTSError):
            tts.get_voice("my_clone")


class MigrationTest(Base):
    def test_old_cosyvoice_voice_and_key_are_migrated(self):
        import json, time as _t
        d = os.path.join(self.tmp.name, "voice_clone"); os.makedirs(d)
        open(os.path.join(d, "20261128-162500-sample.wav"), "wb").write(b"x")
        created = int(_t.mktime(_t.strptime("20261128-162530", "%Y%m%d-%H%M%S")))
        json.dump({"voices": {"my_clone": {"label": "我的声音", "provider": "cosyvoice", "voice_id": "cv-1",
                                           "model": "cosyvoice-v3.5-plus", "created": created}}},
                  open(os.path.join(self.tmp.name, "tts.json"), "w"))
        v = tts.get_voice("my_clone")
        self.assertEqual((v["provider"], v["ids"]), ("aliyun", {"default|cosyvoice-v3.5-plus": "cv-1"}))
        self.assertTrue(v["sample"].endswith("20261128-162500-sample.wav"))
        from core import voice_keys
        with patch.dict(os.environ, {"WXBOT_VOICE_KEYS": os.path.join(self.tmp.name, "k.json"),
                                     "DASHSCOPE_API_KEY": "sk-old"}):
            ks = voice_keys.candidates("aliyun")
            self.assertEqual([(k["key"], k["group"]) for k in ks], [("sk-old", "default")])
            self.assertTrue(oct(os.stat(os.path.join(self.tmp.name, "k.json")).st_mode).endswith("600"))


if __name__ == "__main__":
    unittest.main()
