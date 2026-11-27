"""直发(实验)模块:不连真微信,用假 frida 验证校验、串行和结果判定。"""
import os
import tempfile
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

    def ready(self):
        return {"attached": True, "logged_in": self.logged_in}

    def send(self, to, content):
        self.sent.append((to, content))
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


if __name__ == "__main__":
    unittest.main()
