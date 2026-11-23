"""Refuse WeChat input when the data-dir wxid is not the expected account."""
import os
import unittest
from unittest.mock import patch

from core import docker_wx, sender
from core.moments_native import Native
from core.native_ui import NativeUI


class IdentityGate(unittest.TestCase):
    def test_unset_expected_matches_and_send_continues(self):
        env = {k: v for k, v in os.environ.items() if k != "WXBOT_EXPECTED_WXID"}
        with patch.dict(os.environ, env, clear=True):
            info = docker_wx.require_identity_match()
            self.assertTrue(info["match"])
            self.assertEqual(info["reason"], "unset")
            with patch.object(sender._adapter, "submit", return_value={"ok": True, "status": "submitted"}) as submit:
                out = sender._send("文件传输助手", "filehelper", "text", "hi")
        submit.assert_called_once()
        self.assertEqual(out["status"], "submitted")

    def test_wrong_wxid_raises(self):
        with patch.dict(os.environ, {"WXBOT_EXPECTED_WXID": "wxid_expected"}, clear=False):
            with patch.object(docker_wx, "logged_in", return_value=True), \
                    patch.object(docker_wx, "container_wxid", return_value="wxid_other"):
                with self.assertRaises(docker_wx.IdentityMismatch) as caught:
                    docker_wx.require_identity_match()
        self.assertFalse(caught.exception.info["match"])
        self.assertEqual(caught.exception.info["inferred_wxid"], "wxid_other")
        self.assertEqual(caught.exception.info["expected_wxid"], "wxid_expected")

    def test_native_blocks_click_allows_geometry(self):
        native = Native()
        with patch.dict(os.environ, {"WXBOT_EXPECTED_WXID": "wxid_expected"}, clear=False):
            with patch.object(docker_wx, "logged_in", return_value=True), \
                    patch.object(docker_wx, "container_wxid", return_value="wxid_other"):
                with self.assertRaises(docker_wx.IdentityMismatch):
                    native.run("xdotool", "click", "1")
                with patch("core.moments_native.subprocess.run") as run:
                    run.return_value.returncode = 0
                    run.return_value.stdout = b""
                    native.run("xdotool", "getwindowgeometry", "--shell", "1")
                    with self.assertRaises(docker_wx.IdentityMismatch):
                        native.run("xdotool", "mousemove", "10", "20", "click", "1")
        run.assert_called_once()

    def test_wmctrl_close_blocked_when_mismatch(self):
        native = Native()
        with patch.dict(os.environ, {"WXBOT_EXPECTED_WXID": "wxid_expected"}, clear=False):
            with patch.object(docker_wx, "logged_in", return_value=True), \
                    patch.object(docker_wx, "container_wxid", return_value="wxid_other"), \
                    patch("core.moments_native.subprocess.run") as run:
                with self.assertRaises(docker_wx.IdentityMismatch):
                    native.run("wmctrl", "-ic", "0x1")
        run.assert_not_called()

    def test_wmctrl_close_allowed_when_expected_unset(self):
        native = Native()
        env = {k: v for k, v in os.environ.items() if k != "WXBOT_EXPECTED_WXID"}
        with patch.dict(os.environ, env, clear=True):
            with patch("core.moments_native.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = b""
                native.run("wmctrl", "-ic", "0x1")
                native.run("wmctrl", "-lp")
        self.assertEqual(run.call_count, 2)

    def test_native_ui_blocks_send_once(self):
        ui = NativeUI()
        with patch.dict(os.environ, {"WXBOT_EXPECTED_WXID": "wxid_expected"}, clear=False):
            with patch.object(docker_wx, "logged_in", return_value=True), \
                    patch.object(docker_wx, "container_wxid", return_value="wxid_other"):
                with self.assertRaises(docker_wx.IdentityMismatch):
                    ui.call("send_once")


class ProbeRoute(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def test_probe_token(self):
        env = {k: v for k, v in os.environ.items() if k != "WXBOT_PROBE_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            missing = self.client.get("/api/probe")
        self.assertEqual(missing.status_code, 404)
        with patch.dict(os.environ, {"WXBOT_PROBE_TOKEN": "probe-secret"}, clear=False):
            with patch.object(docker_wx, "require_identity_match", return_value={
                "logged_in": True, "inferred_wxid": "wxid_a", "expected_wxid": "",
                "match": True, "reason": "unset",
            }), patch.object(docker_wx, "wechat_running", return_value=True):
                wrong = self.client.get("/api/probe", headers={"X-Wxbot-Probe-Token": "nope"})
                ok = self.client.get("/api/probe", headers={"X-Wxbot-Probe-Token": "probe-secret"})
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json["match"])
        self.assertTrue(ok.json["wechat_running"])
        self.assertNotIn("probe-secret", ok.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
