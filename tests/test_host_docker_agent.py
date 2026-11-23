"""Host docker agent: peer + token gates, and WeChat via pgrep -f."""
import importlib.util
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "host_docker_agent", ROOT / "tools" / "host_docker_agent.py")
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class _FakeHandler(agent.Handler):
    def __init__(self, peer, body):
        self.client_address = (peer, 5199)
        self.path = "/docker"
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self._code = None
        self._sent = b""

    def send_response(self, code, message=None):
        self._code = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def log_message(self, *args):
        pass


def _post(peer, payload, peers=None, docker=None):
    body = json.dumps(payload).encode("utf-8")
    handler = _FakeHandler(peer, body)
    peers = set() if peers is None else peers
    with mock.patch.object(agent, "container_peer_ips", return_value=peers), \
            mock.patch.object(agent.subprocess, "run", side_effect=docker or (lambda *a, **k: None)):
        handler.do_POST()
    raw = handler.wfile.getvalue()
    return handler._code, json.loads(raw.decode("utf-8") if raw else "{}")


class HostAgentPeerTests(unittest.TestCase):
    def setUp(self):
        self._token = agent.TOKEN
        self._container_token = agent.CONTAINER_TOKEN
        agent.TOKEN = "local-token"
        agent.CONTAINER_TOKEN = "container-token"
        self._env = mock.patch.dict(agent.os.environ, {
            "WXBOT_HOST_AGENT_CONTAINER_NAMES": "wxbot-a,wxbot-b",
        }, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        agent.TOKEN = self._token
        agent.CONTAINER_TOKEN = self._container_token

    def test_loopback_local_token_allowed(self):
        calls = []

        def docker(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "running\n", "")

        code, payload = _post("127.0.0.1", {
            "token": "local-token", "op": "inspect", "container": "wxbot-a",
        }, docker=docker)
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(calls[0][:2], ["docker", "inspect"])

    def test_bridge_peer_local_token_rejected(self):
        code, payload = _post("172.18.0.2", {
            "token": "local-token", "op": "inspect", "container": "wxbot-a",
        }, peers={"172.18.0.2"})
        self.assertEqual(code, 403)
        self.assertFalse(payload["ok"])

    def test_bridge_peer_container_token_matching_container_allowed(self):
        def docker(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, "running\n", "")

        code, payload = _post("172.18.0.2", {
            "token": "container-token", "op": "inspect", "container": "wxbot-a",
        }, peers={"172.18.0.2"}, docker=docker)
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["container"], "wxbot-a")

    def test_bridge_peer_container_token_other_container_rejected(self):
        code, payload = _post("172.18.0.2", {
            "token": "container-token", "op": "stop", "container": "wxbot-other",
        }, peers={"172.18.0.2"})
        self.assertEqual(code, 403)
        self.assertEqual(payload["reason"], "forbidden")

    def test_wechat_op_uses_pgrep_f(self):
        calls = []

        def docker(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "42\n", "")

        code, payload = _post("::1", {
            "token": "local-token", "op": "wechat", "container": "wxbot-a",
        }, docker=docker)
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"ok": True, "container": "wxbot-a", "running": True})
        self.assertEqual(calls, [[
            "docker", "exec", "wxbot-a", "pgrep", "-f", "/opt/wechat/wechat"]])

    def test_stranger_peer_rejected_without_token_in_log(self):
        with mock.patch("builtins.print") as printed:
            code, payload = _post("10.1.2.3", {
                "token": "container-token", "op": "inspect", "container": "wxbot-a",
            }, peers=set())
        self.assertEqual(code, 403)
        text = " ".join(str(c) for c in printed.call_args_list)
        self.assertIn("10.1.2.3", text)
        self.assertNotIn("container-token", text)


class DesktopProbeFallbackTests(unittest.TestCase):
    def test_missing_docker_uses_host_agent_wechat_op(self):
        from core.desktop_management import probe

        profile = {"id": "primary", "name": "当前微信号",
                   "container": "wxbot-ubuntu-manual", "port": 6082}
        calls = []

        def post(op, container):
            calls.append((op, container))
            if op == "inspect":
                return {"ok": True, "running": True, "state": "running"}
            if op == "wechat":
                return {"ok": True, "running": False}
            raise AssertionError(op)

        with mock.patch("core.desktop_management.subprocess.run",
                        side_effect=FileNotFoundError("docker")), \
                mock.patch("core.desktop_management._host_agent_post", side_effect=post):
            state = probe(profile)
        self.assertEqual(calls, [
            ("inspect", "wxbot-ubuntu-manual"),
            ("wechat", "wxbot-ubuntu-manual")])
        self.assertFalse(state["wechat_running"])
        self.assertTrue(state["container_running"])
        self.assertFalse(state["error"])

    def test_unreachable_host_agent_sets_error(self):
        from core.desktop_management import probe

        profile = {"id": "primary", "container": "wxbot-ubuntu-manual", "port": 6082}
        with mock.patch("core.desktop_management.subprocess.run",
                        side_effect=FileNotFoundError("docker")), \
                mock.patch("core.desktop_management._host_agent_post", return_value=None):
            state = probe(profile)
        self.assertFalse(state["wechat_running"])
        self.assertEqual(state["error"], "宿主状态服务不可用")


if __name__ == "__main__":
    unittest.main()
