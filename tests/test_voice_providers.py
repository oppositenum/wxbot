import base64
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from core import voice_providers as vp
from core.voice_providers import ProviderError


def resp(status, body, headers=None):
    return status, body if isinstance(body, bytes) else json.dumps(body).encode(), headers or {}


class ClassifyTest(unittest.TestCase):
    def test_aliyun(self):
        c = vp.Aliyun.classify
        self.assertEqual(c(401, "InvalidApiKey", ""), "auth")
        self.assertEqual(c(400, "Arrearage", "Access denied, please make sure your account is in good standing."), "exhausted")
        self.assertEqual(c(429, "Throttling.RateQuota", ""), "rate")
        self.assertEqual(c(429, "Throttling.AllocationQuota", "您当前的配额为 1000"), "bad_input")
        self.assertEqual(c(403, "AllocationQuota.FreeTierOnly", "free tier exhausted"), "exhausted")

    def test_minimax(self):
        c = vp.MiniMax.classify
        self.assertEqual([c(x, "") for x in (1002, 1004, 1008, 2038, 2013, 1000)],
                         ["rate", "auth", "exhausted", "auth", "bad_input", "other"])

    def test_elevenlabs(self):
        c = vp.ElevenLabs.classify
        self.assertEqual([c(x, "") for x in (401, 402, 429, 404, 422, 500)],
                         ["auth", "exhausted", "rate", "voice_missing", "bad_input", "other"])

    def test_volcengine(self):
        c = vp.Volcengine.classify
        self.assertEqual(c(429, 0, ""), "rate")
        self.assertEqual(c(200, 45000000, "quota exceeded for types: concurrency"), "rate")
        self.assertEqual(c(403, 0, "account balance insufficient"), "exhausted")
        self.assertEqual(c(200, 45000000, "speaker permission denied: get resource id: access denied"), "voice_missing")
        self.assertEqual(c(401, 0, "invalid api key"), "auth")


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wav = os.path.join(self.tmp.name, "s.wav")
        open(self.wav, "wb").write(b"RIFFdata")

    def tearDown(self):
        self.tmp.cleanup()

    def test_minimax_upload_clone_and_hex_audio(self):
        calls = []

        def http(method, url, headers=None, data=None, timeout=None):
            calls.append((url, headers, data))
            if url.endswith("/v1/files/upload"):
                return resp(200, {"file": {"file_id": 123}, "base_resp": {"status_code": 0}})
            if url.endswith("/v1/voice_clone"):
                return resp(200, {"base_resp": {"status_code": 0, "status_msg": "success"}})
            return resp(200, {"data": {"audio": b"ID3".hex()}, "base_resp": {"status_code": 0}})
        with patch.object(vp, "_http", side_effect=http):
            vid = vp.MiniMax().enroll("K", self.wav, "n", "speech-2.8-hd")
            audio = vp.MiniMax().synth("K", "你好", vid, "speech-2.8-hd")
        self.assertEqual(audio, b"ID3")
        self.assertIn(b'name="purpose"', calls[0][2])
        body = json.loads(calls[1][2])
        self.assertEqual((body["file_id"], body["voice_id"]), (123, vid))
        self.assertTrue(vid[0].isalpha() and len(vid) >= 8)
        self.assertEqual(calls[0][1]["Authorization"], "Bearer K")

    def test_minimax_balance_error_is_exhausted(self):
        with patch.object(vp, "_http", return_value=resp(200, {"base_resp": {"status_code": 1008, "status_msg": "insufficient balance"}})):
            with self.assertRaises(ProviderError) as cm:
                vp.MiniMax().synth("K", "hi", "v", "speech-2.8-hd")
        self.assertEqual(cm.exception.kind, "exhausted")

    def test_volcengine_clone_polls_until_ready_and_stream_is_joined(self):
        seq = [resp(200, {"status": 1}), resp(200, {"status": 1}), resp(200, {"status": 2})]
        sent = []

        def http(method, url, headers=None, data=None, timeout=None):
            sent.append((url, headers, json.loads(data)))
            if url.endswith("/unidirectional"):
                lines = [{"code": 0, "data": base64.b64encode(b"AB").decode()},
                         {"code": 0, "data": base64.b64encode(b"CD").decode()},
                         {"code": 20000000, "message": "ok", "data": None}]
                return resp(200, "\n".join(json.dumps(x) for x in lines).encode())
            return seq.pop(0)
        with patch.object(vp, "_http", side_effect=http), patch.object(vp.time, "sleep"):
            vid = vp.Volcengine().enroll("K", self.wav, "n", "seed-icl-2.0")
            audio = vp.Volcengine().synth("K", "你好", vid, "seed-icl-2.0")
        self.assertEqual(audio, b"ABCD")
        clone = sent[0][2]
        self.assertEqual((clone["speaker_id"], clone["custom_speaker_id"]), ("custom_speaker_id", vid))
        self.assertEqual(sent[1][2], {"speaker_id": "custom_speaker_id", "custom_speaker_id": vid})
        self.assertEqual(sent[-1][1]["X-Api-Resource-Id"], "seed-icl-2.0")
        self.assertEqual(sent[-1][2]["req_params"]["speaker"], vid)

    def test_volcengine_purchased_slot(self):
        with patch.object(vp, "_http", return_value=resp(200, {"status": 2})) as h:
            vid = vp.Volcengine().enroll("K", self.wav, "n", "seed-icl-2.0", {"speaker_id": "S_abc"})
        self.assertEqual(vid, "S_abc")
        self.assertEqual(json.loads(h.call_args.kwargs["data"])["speaker_id"], "S_abc")

    def test_elevenlabs_payment_required(self):
        with patch.object(vp, "_http", return_value=resp(402, {"detail": {"status": "payment_required"}})):
            with self.assertRaises(ProviderError) as cm:
                vp.ElevenLabs().synth("K", "hi", "v", "eleven_v4")
        self.assertEqual(cm.exception.kind, "exhausted")

    def test_aliyun_enroll_data_uri(self):
        with patch.object(vp, "_http", return_value=resp(200, {"output": {"voice_id": "cv-9"}})) as h:
            vid = vp.Aliyun().enroll("K", self.wav, "my_clone", "cosyvoice-v3-flash")
        inp = json.loads(h.call_args.kwargs["data"])["input"]
        self.assertEqual((vid, inp["target_model"], inp["prefix"]), ("cv-9", "cosyvoice-v3-flash", "myclone"))
        self.assertTrue(inp["url"].startswith("data:audio/wav;base64,"))


class KeysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"WXBOT_VOICE_KEYS": os.path.join(self.tmp.name, "k.json")})
        self.env.start()
        from core import voice_keys
        self.vk = voice_keys

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def test_round_robin_and_states(self):
        a = self.vk.add("minimax", "AAAAAAAAAAAAAAAA", "a")
        b = self.vk.add("minimax", "BBBBBBBBBBBBBBBB", "b")
        firsts = [self.vk.candidates("minimax")[0]["id"] for _ in range(4)]
        self.assertEqual(firsts, [a, b, a, b])
        self.vk.report("minimax", a, "rate", "1002")
        self.assertEqual([k["id"] for k in self.vk.candidates("minimax")], [b])
        self.vk.report("minimax", b, "auth", "bad key")
        self.assertEqual(self.vk.candidates("minimax"), [])
        v = {k["id"]: k for k in self.vk.view("minimax")["minimax"]["keys"]}
        self.assertEqual((v[a]["status"], v[b]["status"]), ("rate", "auth"))
        self.assertEqual(v[a]["masked"], "AAAA…AAAA")
        self.vk.update("minimax", b, reset=True)
        self.assertEqual([k["id"] for k in self.vk.candidates("minimax")], [b])
        with self.assertRaises(ValueError):
            self.vk.add("minimax", "AAAAAAAAAAAAAAAA")

    def test_run_rotates_and_raises_other(self):
        self.vk.add("minimax", "A" * 16)
        self.vk.add("minimax", "B" * 16)
        seen = []

        def fn(k):
            seen.append(k["key"][0])
            if len(seen) == 1:
                raise ProviderError("exhausted", "1008")
            return "ok"
        self.assertEqual(self.vk.run("minimax", fn), "ok")
        self.assertEqual(len(seen), 2)

        def bad(k):
            raise ProviderError("bad_input", "文本不合法")
        with self.assertRaises(ProviderError):
            self.vk.run("minimax", bad)

        def dead(k):
            raise ProviderError("auth", "x")
        with self.assertRaises(self.vk.NoKeyError):
            self.vk.run("minimax", dead)


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"WXBOT_VOICE_KEYS": os.path.join(self.tmp.name, "k.json"),
                                           "WXBOT_UI_AUTH": "1", "WXBOT_UI_USER": "u", "WXBOT_UI_PASSWORD": "p",
                                           "WXBOT_SECRET": "s", "WXBOT_API_TOKEN": "api-token"})
        self.env.start()
        import server
        self.c = server.app.test_client()

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def test_keys_need_login_and_are_masked(self):
        r = self.c.post("/api/voice/keys", json={"provider": "minimax", "action": "add", "key": "SECRETSECRET1234"},
                        headers={"Authorization": "Bearer api-token"})
        self.assertEqual(r.status_code, 401)
        self.c.post("/api/auth/login", json={"username": "u", "password": "p"})
        r = self.c.post("/api/voice/keys", json={"provider": "minimax", "action": "add", "key": "SECRETSECRET1234",
                                                  "group": "acct"})
        body = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("SECRETSECRET1234", body)
        mm = [p for p in r.get_json()["providers"] if p["id"] == "minimax"][0]
        self.assertEqual((mm["keys"][0]["masked"], mm["keys"][0]["group"]), ("SECR…1234", "acct"))
        self.assertFalse([p for p in r.get_json()["providers"] if p["id"] == "volcengine"][0]["auto_enroll"])


if __name__ == "__main__":
    unittest.main()
