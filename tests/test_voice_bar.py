import os
import shutil
import subprocess
import tempfile
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import patch

from core import voice_bar
from core.legacy_sender import LegacySearchAdapter


@unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
class ToWavTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = patch("config.WORK_DIR", self.tmp.name)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def _tone(self, secs):
        src = os.path.join(self.tmp.name, "a.mp3")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=duration={secs}", src],
                       check=True)
        return src

    def test_pads_and_converts_to_48k_mono(self):
        wav, secs = voice_bar.to_wav(self._tone(2))
        with wave.open(wav, "rb") as w:
            self.assertEqual((w.getnchannels(), w.getframerate(), w.getsampwidth()), (1, 48000, 2))
        self.assertAlmostEqual(secs, 2 + voice_bar.LEAD_IN + voice_bar.TAIL, delta=0.15)

    def test_caps_at_wechat_limit(self):
        _wav, secs = voice_bar.to_wav(self._tone(70))
        self.assertLessEqual(secs, voice_bar.MAX_SECONDS + 0.1)

    def test_rejects_too_short(self):
        with self.assertRaises(voice_bar.VoiceBarError) as cm:
            voice_bar.to_wav(self._tone(0.2))
        self.assertEqual(cm.exception.code, "too_short")


class SendAudioTest(unittest.TestCase):
    def test_filehelper_refused(self):
        with self.assertRaises(voice_bar.VoiceBarError):
            voice_bar.send_audio("filehelper", "/nope.mp3")

    def test_passes_display_and_wav_to_sender(self):
        with patch("core.voice_bar.to_wav", return_value=("/w/x.wav", 3.2)), \
                patch("core.sender.send_voice_bar", return_value={"ok": True, "status": "submitted"}) as s:
            r = voice_bar.send_audio("wxid_a", "/a.mp3", "小A")
        s.assert_called_once_with("小A", "/w/x.wav", "wxid_a")
        self.assertEqual(r["seconds"], 3.2)


class LegacyVoiceTest(unittest.TestCase):
    def test_capability_and_timeout(self):
        a = LegacySearchAdapter()
        self.assertEqual(a.capability("wxid_a", "voice"), "")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "v.wav")
            with wave.open(p, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
                w.writeframes(b"\0\0" * 48000 * 10)
            self.assertEqual(LegacySearchAdapter._timeout("voice", p), 70)
        self.assertEqual(LegacySearchAdapter._timeout("text", "x"), 40)


if __name__ == "__main__":
    unittest.main()
