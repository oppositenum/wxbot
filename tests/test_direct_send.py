"""直发(实验)模块:不连真微信,用假 frida 验证校验、串行和结果判定。"""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from core import direct_send as ds

GOOD = next(iter(ds.SUPPORTED_SHA256))


class FakeExports:
    def __init__(self, send_result=None, steps=(9,), logged_in=True):
        self.send_result = send_result or {"ok": True, "calls": 0}
        self.steps = list(steps)
        self.logged_in = logged_in
        self.sent = []
        self.opts = []

    def ready(self):
        return {"attached": True, "logged_in": self.logged_in}

    def send(self, to, content, opts=None):
        self.sent.append((to, content))
        self.opts.append(opts or {})
        return self.send_result

    def send_image(self, to, path, size):
        self.sent.append(("image", to, path, size))
        return self.send_result

    def poll(self):
        return {"step": self.steps.pop(0) if self.steps else 0, "calls": 1}


class FakeScript:
    def __init__(self, exports):
        self.exports_sync = exports

    def on(self, *a):
        pass

    def load(self):
        pass


class FakeSession:
    is_detached = False

    def __init__(self, script):
        self.script = script

    def create_script(self, src):
        return self.script

    def detach(self):
        self.is_detached = True


class FakeProc:
    def __init__(self, pid, name):
        self.pid, self.name = pid, name


class FakeDevice:
    def __init__(self, script, pid=4242):
        self.script, self.pid, self.attached = script, pid, 0

    def enumerate_processes(self):
        return [FakeProc(1, "Xvfb")] + ([FakeProc(self.pid, "wechat")] if self.pid else [])

    def attach(self, pid):
        self.attached += 1
        return FakeSession(self.script)


class FakeFrida:
    def __init__(self, dev):
        self.dev = dev

    def get_local_device(self):
        return self.dev


class DirectSendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.exports = FakeExports()
        self.dev = FakeDevice(FakeScript(self.exports))
        ds._drop()
        self.patches = [
            patch.object(ds, "HISTORY", os.path.join(self.tmp.name, "h.jsonl")),
            patch.object(ds, "_import_frida", return_value=FakeFrida(self.dev)),
            patch.object(ds, "binary_version", return_value=(GOOD, "4.1.8 aarch64")),
            patch.object(ds, "WAIT_SECONDS", 0.5),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        ds._drop()
        self.tmp.cleanup()

    def test_success_is_recorded_and_session_is_reused(self):
        row = ds.send("wxid_a", "你好 👋", name="甲")
        self.assertTrue(row["ok"])
        self.exports.steps = [9]
        ds.send("1@chatroom", "群里好")
        self.assertEqual(self.exports.sent, [("wxid_a", "你好 👋"), ("1@chatroom", "群里好")])
        self.assertEqual(self.dev.attached, 1)
        h = ds.history()
        self.assertEqual([x["chat"] for x in h], ["1@chatroom", "wxid_a"])
        self.assertTrue(all(x["ok"] for x in h))

    def test_reattaches_when_wechat_restarts(self):
        ds.send("wxid_a", "1")
        self.dev.pid = 5555
        self.exports.steps = [9]
        ds.send("wxid_a", "2")
        self.assertEqual(self.dev.attached, 2)

    def test_rejects_bad_input_without_calling_wechat(self):
        for chat, content, code in [("", "x", "bad_chat"), ("wxid a", "x", "bad_chat"),
                                    ("wxid_a", "  ", "empty"), ("wxid_a", "x" * 2001, "too_long")]:
            with self.assertRaises(ds.DirectSendError) as cm:
                ds.send(chat, content)
            self.assertEqual(cm.exception.code, code)
        self.assertEqual(self.exports.sent, [])

    def test_unsupported_binary_refuses_to_attach(self):
        with patch.object(ds, "binary_version", return_value=("deadbeef" * 8, None)):
            with self.assertRaises(ds.DirectSendError) as cm:
                ds.send("wxid_a", "x")
        self.assertEqual(cm.exception.code, "unsupported")
        self.assertEqual(self.dev.attached, 0)

    def test_not_logged_in_and_failed_step_and_timeout(self):
        self.exports.send_result = {"ok": False, "error": "not_logged_in"}
        with self.assertRaises(ds.DirectSendError) as cm:
            ds.send("wxid_a", "x")
        self.assertEqual(cm.exception.code, "not_logged_in")
        self.exports.send_result = {"ok": True, "calls": 0}
        self.exports.steps = [4]
        with self.assertRaises(ds.DirectSendError) as cm:
            ds.send("wxid_a", "x")
        self.assertEqual(cm.exception.code, "step_failed")
        self.exports.steps = []
        with self.assertRaises(ds.DirectSendError) as cm:
            ds.send("wxid_a", "x")
        self.assertEqual(cm.exception.code, "timeout")
        self.assertEqual([x["ok"] for x in ds.history()], [False, False, False])

    def test_image_is_staged_checked_and_sent_with_size(self):
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 100
        with patch.object(ds, "MEDIA_DIR", os.path.join(self.tmp.name, "media")):
            path = ds.stage_image(png, "a.png")
            row = ds.send_image("wxid_a", path, name="甲", label="a.png")
            self.assertTrue(row["ok"])
            self.assertEqual(self.exports.sent, [("image", "wxid_a", os.path.realpath(path), len(png))])
            self.assertEqual(ds.history()[0]["kind"], "image")
            for data, code in [(b"", "empty"), (b"not an image at all", "bad_image")]:
                with self.assertRaises(ds.DirectSendError) as cm:
                    ds.stage_image(data)
                self.assertEqual(cm.exception.code, code)
        with self.assertRaises(ds.DirectSendError) as cm:
            ds.send_image("wxid_a", os.path.join(self.tmp.name, "missing.png"))
        self.assertEqual(cm.exception.code, "no_file")

    def test_wechat_not_running(self):
        self.dev.pid = None
        st = ds.status()
        self.assertFalse(st["attached"])
        self.assertEqual(st["code"], "not_running")


class DirectSendRoutesTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "WXBOT_UI_AUTH": "1", "WXBOT_UI_USER": "u", "WXBOT_UI_PASSWORD": "p",
            "WXBOT_SECRET": "s", "WXBOT_API_TOKEN": "api-token"}, clear=False)
        self.env.start()
        import server
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def tearDown(self):
        self.env.stop()

    def test_requires_ui_login_and_api_token_is_not_enough(self):
        self.assertEqual(self.client.post("/api/direct/send", json={"chat": "a", "content": "x"}).status_code, 401)
        r = self.client.post("/api/direct/send", json={"token": "api-token", "chat": "a", "content": "x"})
        self.assertEqual(r.status_code, 401)

    def test_send_mode_requires_login_and_validates(self):
        self.assertEqual(self.client.get("/api/send_mode").status_code, 401)
        self.assertEqual(self.client.post("/api/send_mode", json={"token": "api-token", "mode": "direct"}).status_code, 401)
        self.client.post("/api/auth/login", json={"username": "u", "password": "p"})
        with tempfile.TemporaryDirectory() as d, patch("config.WORK_DIR", d):
            self.assertEqual(self.client.get("/api/send_mode").json["mode"], "ui")
            self.assertEqual(self.client.post("/api/send_mode", json={"mode": "bad"}).status_code, 400)
            r = self.client.post("/api/send_mode", json={"mode": "direct", "fallback": False})
            self.assertEqual((r.json["mode"], r.json["fallback"]), ("direct", False))
            self.assertEqual(self.client.get("/api/send_mode").json["mode"], "direct")

    def test_send_maps_errors_to_409(self):
        self.client.post("/api/auth/login", json={"username": "u", "password": "p"})
        with patch("core.direct_send.send", side_effect=ds.DirectSendError("empty", "内容不能为空")):
            r = self.client.post("/api/direct/send", json={"chat": "a", "content": ""})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json["code"], "empty")
        with patch("core.direct_send.send", return_value={"ok": True, "chat": "a"}):
            r = self.client.post("/api/direct/send", json={"chat": "a", "content": "hi"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json["ok"])

    def test_send_image_path_is_limited_to_upload_dirs(self):
        self.client.post("/api/auth/login", json={"username": "u", "password": "p"})
        with patch("core.direct_send.send_image") as send:
            r = self.client.post("/api/direct/send_image", json={"chat": "a", "path": "/etc/passwd"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json["code"], "bad_path")
        send.assert_not_called()

    def test_send_image_multipart_stages_then_sends(self):
        import io
        self.client.post("/api/auth/login", json={"username": "u", "password": "p"})
        with patch("core.direct_send.stage_image", return_value="/w/direct_media/x.png") as stage, \
             patch("core.direct_send.send_image", return_value={"ok": True}) as send:
            r = self.client.post("/api/direct/send_image", content_type="multipart/form-data", data={
                "chat": "1@chatroom", "name": "群", "file": (io.BytesIO(b"\x89PNG\r\n\x1a\nxx"), "p.png")})
        self.assertEqual(r.status_code, 200)
        stage.assert_called_once()
        send.assert_called_once_with("1@chatroom", "/w/direct_media/x.png", "群", "p.png")

    def test_non_media_upload_goes_out_as_file_card(self):
        import io
        self.client.post("/api/auth/login", json={"username": "u", "password": "p"})
        with patch("core.direct_send.stage_file", return_value="/w/direct_files/报告.pdf") as stage, \
             patch("core.direct_send.send_file", return_value={"ok": True}) as send:
            r = self.client.post("/api/direct/send_image", content_type="multipart/form-data", data={
                "chat": "1@chatroom", "name": "群", "file": (io.BytesIO(b"%PDF-1.7 xx"), "报告.pdf")})
        self.assertEqual(r.status_code, 200)
        stage.assert_called_once_with(b"%PDF-1.7 xx", "报告.pdf")
        send.assert_called_once_with("1@chatroom", "/w/direct_files/报告.pdf", "群", "报告.pdf")


if __name__ == "__main__":
    unittest.main()


class LenientDbTextTest(unittest.TestCase):
    def test_invalid_utf8_text_does_not_break_reads(self):
        import sqlite3
        from core import db
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.db")
            raw = sqlite3.connect(path)
            raw.execute("CREATE TABLE Name2Id(user_name TEXT)")
            raw.execute("INSERT INTO Name2Id VALUES (?)", ("wxid_ok",))
            raw.execute("INSERT INTO Name2Id VALUES (CAST(? AS TEXT))", (b" \xfb#\x8c\xff\xff",))
            raw.commit(); raw.close()
            with patch("config.decrypted_path", return_value=path):
                con = db.connect("message")
                names = [r[0] for r in con.execute("SELECT user_name FROM Name2Id ORDER BY rowid")]
                con.close()
        self.assertEqual(names[0], "wxid_ok")
        self.assertIn("�", names[1])


class DirectSendVideoTest(unittest.TestCase):
    def test_media_kind_detects_mp4_and_images(self):
        from core import direct_send
        self.assertEqual(direct_send.media_kind(b"\x00\x00\x00\x20ftypisom\x00\x00"), "video")
        self.assertEqual(direct_send.media_kind(b"\x00\x00\x00\x14ftypqt  \x00\x00"), "video")
        self.assertEqual(direct_send.media_kind(b"\x89PNG\r\n\x1a\n"), "image")
        self.assertIsNone(direct_send.media_kind(b"hello world....."))

    def test_send_video_passes_size_to_agent(self):
        from core import direct_send
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "v.mp4")
            with open(path, "wb") as f:
                f.write(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100)
            calls = []

            class Ex:
                def send_video(self, chat, p, size):
                    calls.append((chat, p, size))
                    return {"ok": True}

            with patch("core.direct_send._dispatch", side_effect=lambda row, call: (call(Ex()), row)[1]):
                row = direct_send.send_video("filehelper", path)
            self.assertEqual(calls, [("filehelper", os.path.realpath(path), 112)])
            self.assertEqual(row["kind"], "video")

    def test_send_video_rejects_non_video(self):
        from core import direct_send
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "v.mp4")
            with open(path, "wb") as f:
                f.write(b"not a video at all")
            with self.assertRaises(direct_send.DirectSendError) as cm:
                direct_send.send_video("filehelper", path)
            self.assertEqual(cm.exception.code, "bad_video")


class DirectRevokeTest(unittest.TestCase):
    ME, PEER = "wxid_me", "wxid_peer"

    def _db(self, d, rows):
        import hashlib
        import sqlite3
        path = os.path.join(d, "m.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE Name2Id(user_name TEXT)")
        con.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, ?)", (self.ME,))
        con.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (2, ?)", (self.PEER,))
        t = "Msg_" + hashlib.md5(self.PEER.encode()).hexdigest()
        con.execute(f"CREATE TABLE {t}(local_id INTEGER, server_id INTEGER, local_type INTEGER, sort_seq INTEGER,"
                    " real_sender_id INTEGER, create_time INTEGER, status INTEGER, message_content TEXT, source TEXT)")
        for r in rows:
            con.execute(f"INSERT INTO {t} VALUES (?,?,?,?,?,?,?,?,?)", r)
        con.commit()
        con.close()
        return lambda *_a, **_k: sqlite3.connect(path)

    def _patches(self, connect):
        return patch("core.db.connect", side_effect=connect), patch("config.wxid", return_value=self.ME)

    def test_finds_my_latest_message_and_skips_peer_and_revoked(self):
        from core import direct_send
        now = int(time.time())
        with tempfile.TemporaryDirectory() as d:
            conn = self._db(d, [
                (10, -5, 1, now * 1000, 1, now - 5, 2, "mine", "<s/>"),
                (11, 7, 1, now * 1000 + 1, 2, now - 3, 3, "theirs", ""),
                (12, 8, 10000, now * 1000 + 2, 1, now - 1, 4, "revoked", ""),
            ])
            a, b = self._patches(conn)
            with a, b:
                m = direct_send._find_my_message(self.PEER)
        self.assertEqual((m["localId"], m["content"], m["sender"]), (10, "mine", self.ME))
        self.assertEqual(m["serverId"], str((-5) & 0xFFFFFFFFFFFFFFFF))   # 负数 server_id 按 u64 传

    def test_rejects_peer_message_and_old_message(self):
        from core import direct_send
        now = int(time.time())
        with tempfile.TemporaryDirectory() as d:
            conn = self._db(d, [(11, 7, 1, 1, 2, now, 3, "theirs", ""), (10, 6, 1, 0, 1, now - 600, 2, "old", "")])
            a, b = self._patches(conn)
            with a, b:
                with self.assertRaises(direct_send.DirectSendError) as cm:
                    direct_send._find_my_message(self.PEER, 11)
                self.assertEqual(cm.exception.code, "not_mine")
                with self.assertRaises(direct_send.DirectSendError) as cm:
                    direct_send.revoke(self.PEER, 10)
                self.assertEqual(cm.exception.code, "too_old")

    def test_filehelper_cannot_revoke(self):
        from core import direct_send
        with self.assertRaises(direct_send.DirectSendError) as cm:
            direct_send.revoke("filehelper")
        self.assertEqual(cm.exception.code, "bad_chat")

    def test_revoke_reports_wechat_refusal(self):
        from core import direct_send
        now = int(time.time())
        with tempfile.TemporaryDirectory() as d:
            conn = self._db(d, [(10, 6, 1, 0, 1, now, 2, "x", "")])
            a, b = self._patches(conn)

            class Script:
                class exports_sync:
                    @staticmethod
                    def revoke(msg):
                        return {"ok": True}

                    @staticmethod
                    def poll():
                        return {"step": 9, "result": 0}

            with a, b, patch("core.direct_send._ensure_script", return_value=Script), \
                    patch("core.direct_send._record"):
                with self.assertRaises(direct_send.DirectSendError) as cm:
                    direct_send.revoke(self.PEER)
        self.assertEqual(cm.exception.code, "revoke_failed")


    def test_batch_revoke_reads_low_byte_of_result(self):
        # 4.1.13 批量撤回的结果首字节是成功标志,其余字节是未初始化的 0xaa
        from core import direct_send
        now = int(time.time())
        for raw, ok in (("aaaaaaaaaaaaaa01", True), ("aaaaaaaaaaaaaa00", False)):
            with tempfile.TemporaryDirectory() as d:
                conn = self._db(d, [(10, 6, 1, 0, 1, now, 2, "x", "")])
                a, b = self._patches(conn)

                class Script:
                    class exports_sync:
                        @staticmethod
                        def revoke(msg):
                            return {"ok": True}

                        @staticmethod
                        def poll():
                            return {"step": 9, "result": 1, "raw": [raw, "0", "0"]}

                with a, b, patch("core.direct_send._ensure_script", return_value=Script), \
                        patch("core.direct_send._record"):
                    if ok:
                        self.assertTrue(direct_send.revoke(self.PEER)["ok"])
                    else:
                        with self.assertRaises(direct_send.DirectSendError):
                            direct_send.revoke(self.PEER)


class MentionQuoteTest(unittest.TestCase):
    MEMBERS = {"wxid_b": "黎深", "wxid_c": "黎", "wxid_d": "大蜜"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.exports = FakeExports()
        self.dev = FakeDevice(FakeScript(self.exports))
        ds._drop()
        self.patches = [
            patch.object(ds, "HISTORY", os.path.join(self.tmp.name, "h.jsonl")),
            patch.object(ds, "_import_frida", return_value=FakeFrida(self.dev)),
            patch.object(ds, "binary_version", return_value=(GOOD, "4.1.8 aarch64")),
            patch.object(ds, "WAIT_SECONDS", 0.5),
            patch.object(ds, "_member_names", return_value=dict(self.MEMBERS)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        ds._drop()
        self.tmp.cleanup()

    def test_resolve_mentions_longest_name_and_separator(self):
        text, ids = ds.resolve_mentions("g@chatroom", "@黎深 你好 @黎 嗨 @陌生人 @黎深")
        self.assertEqual(text, "@黎深\u2005你好 @黎\u2005嗨 @陌生人 @黎深\u2005")
        self.assertEqual(ids, ["wxid_b", "wxid_c"])
        self.assertEqual(ds.resolve_mentions("wxid_a", "@黎深 你好"), ("@黎深 你好", []))

    def test_mention_text_prepends_missing_names_only(self):
        text, ids = ds.mention_text("g@chatroom", ["wxid_d", "wxid_b"], "@黎深 吃饭了")
        self.assertEqual(text, "@大蜜\u2005@黎深\u2005吃饭了")
        self.assertEqual(ids, ["wxid_d", "wxid_b"])
        with self.assertRaises(ds.DirectSendError) as cm:
            ds.mention_text("g@chatroom", ["wxid a"], "x")
        self.assertEqual(cm.exception.code, "bad_at")

    def test_send_passes_at_and_quote_to_agent(self):
        with patch.object(ds, "_find_message", return_value={"localId": 7, "createTime": 123, "sender": "wxid_b", "type": 1}):
            row = ds.send("g@chatroom", "收到", at=["wxid_b"], quote=7)
        self.assertEqual(self.exports.sent, [("g@chatroom", "@黎深\u2005收到")])
        self.assertEqual(self.exports.opts[0], {"at": ["wxid_b"], "quote": {"localId": 7, "createTime": 123, "name": "黎深"}})
        self.assertEqual((row["kind"], row["quote"], row["at"]), ("quote", 7, ["wxid_b"]))

    def test_auto_mentions_only_when_version_supports_at(self):
        ds.send("g@chatroom", "@大蜜 好", at="auto")
        self.assertEqual(self.exports.opts[-1], {})          # 4.1.8 没有 @ 字段:原样发
        self.exports.steps = [9]
        with patch.object(ds, "at_supported", return_value=True):
            ds.send("g@chatroom", "@大蜜 好", at="auto")
        self.assertEqual(self.exports.sent[-1], ("g@chatroom", "@大蜜\u2005好"))
        self.assertEqual(self.exports.opts[-1], {"at": ["wxid_d"]})

    def test_at_needs_group(self):
        with self.assertRaises(ds.DirectSendError) as cm:
            ds.send("wxid_a", "hi", at=["wxid_b"])
        self.assertEqual(cm.exception.code, "bad_at")
        self.assertEqual(self.exports.sent, [])


class DirectRevokeRouteTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "WXBOT_UI_AUTH": "1", "WXBOT_UI_USER": "u", "WXBOT_UI_PASSWORD": "p",
            "WXBOT_SECRET": "s", "WXBOT_API_TOKEN": "api-token"}, clear=False)
        self.env.start()
        import server
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def tearDown(self):
        self.env.stop()

    def test_token_allowed_and_local_id_parsed(self):
        self.assertEqual(self.client.post("/api/direct/revoke", json={"chat": "a"}).status_code, 401)
        with patch("core.direct_send.revoke", return_value={"ok": True}) as rv:
            r = self.client.post("/api/direct/revoke", json={"token": "api-token", "chat": "wxid_a", "local_id": "12"})
        self.assertEqual(r.status_code, 200)
        rv.assert_called_once_with("wxid_a", 12, "", source="manual")


class DirectVoiceTest(unittest.TestCase):
    def test_audio_kinds(self):
        from core import direct_send
        self.assertEqual(direct_send.media_kind(b"ID3\x04\x00" + b"\x00" * 11), "audio")
        self.assertEqual(direct_send.media_kind(b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00"), "audio")
        self.assertEqual(direct_send.media_kind(b"RIFF\x00\x00\x00\x00WAVEfmt "), "audio")
        self.assertEqual(direct_send.media_kind(b"\x00\x00\x00\x20ftypisom\x00\x00\x00\x00"), "video")

    def test_silk_is_rejected_with_hint(self):
        from core import direct_send
        with self.assertRaises(direct_send.DirectSendError) as cm:
            direct_send.stage_media(b"\x02#!SILK_V3" + b"\x00" * 20)
        self.assertEqual(cm.exception.code, "bad_audio")

    @unittest.skipUnless(__import__("shutil").which("ffmpeg"), "需要 ffmpeg")
    def test_audio_becomes_mp4(self):
        import subprocess
        from core import direct_send
        with tempfile.TemporaryDirectory() as d, patch("core.direct_send.MEDIA_DIR", d):
            wav = os.path.join(d, "a.wav")
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                            wav], check=True)
            kind, path = direct_send.stage_media(open(wav, "rb").read(), "a.wav")
            self.assertEqual(kind, "voice")
            with open(path, "rb") as f:
                self.assertEqual(direct_send.media_kind(f.read(16)), "video")


class DirectVoiceMsgTest(unittest.TestCase):
    def test_voice_unsupported_on_418_profile(self):
        from core import direct_send
        with patch("core.direct_send.binary_version",
                   return_value=("e40116597803a5e0d9fc4a2b727df4b8f7a3dd6de1c336140ea373be2b6626e9", "4.1.8")):
            self.assertFalse(direct_send.voice_supported())

    @unittest.skipUnless(__import__("importlib").util.find_spec("pilk") and __import__("shutil").which("ffmpeg"),
                         "需要 pilk 和 ffmpeg")
    def test_encode_silk_header_and_duration(self):
        import subprocess
        from core import direct_send
        with tempfile.TemporaryDirectory() as d, patch("core.direct_send.MEDIA_DIR", d):
            wav = os.path.join(d, "a.wav")
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=2", wav], check=True)
            silk, ms = direct_send.encode_silk(wav)
            self.assertTrue(open(silk, "rb").read().startswith(b"\x02#!SILK_V3"))
            self.assertAlmostEqual(ms, 2000, delta=60)
