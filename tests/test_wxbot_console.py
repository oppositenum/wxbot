"""Host console registry: adopt only, no start/stop, docker stubbed."""
import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "wxbot_console", ROOT / "tools" / "wxbot_console.py")
console = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(console)


def _completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class ConsoleRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.id().replace(".", "_") + "_registry.json")
        # Isolate from the real ~/.wxbot tree.
        self._reg = console.REGISTRY
        console.REGISTRY = Path(__file__).resolve().parent / self.tmp.name
        if console.REGISTRY.exists():
            console.REGISTRY.unlink()

    def tearDown(self):
        for path in (console.REGISTRY, console.REGISTRY.with_suffix(".tmp")):
            if path.exists():
                path.unlink()
        console.REGISTRY = self._reg

    def _docker(self, known):
        def run(args, timeout=None, **kwargs):
            cmd = list(args)
            self.assertEqual(cmd[0], "docker")
            name = next((part for part in reversed(cmd[1:]) if not str(part).startswith("-") and "{{" not in str(part)), "")
            if cmd[1] == "inspect":
                if name not in known:
                    return _completed(cmd, 1, stderr="No such object: " + name)
                meta = known[name]
                binds = json.dumps([meta.get("volume_src", "/var/lib/docker/volumes/x/_data")])
                hostname = meta.get("hostname", "wx")
                return _completed(cmd, 0, stdout=f"{binds}\t{hostname}\n")
            if cmd[1] == "port":
                if name not in known:
                    return _completed(cmd, 1, stderr="No such container")
                return _completed(cmd, 0, stdout=known[name].get("ports", ""))
            if cmd[1] == "ps":
                wanted = ""
                if "--filter" in cmd:
                    wanted = cmd[cmd.index("--filter") + 1].split("=", 1)[-1].strip("^$")
                running = [
                    n for n, meta in known.items()
                    if meta.get("running") and (not wanted or n == wanted or n.startswith(wanted))
                ]
                return _completed(cmd, 0, stdout="".join(n + "\n" for n in running))
            raise AssertionError(cmd)
        return run

    def test_adopt_refuses_unknown_container(self):
        with mock.patch.object(console.subprocess, "run", side_effect=self._docker({})):
            code, payload = console.adopt("wxbot-missing", "甲", "wxid_a")
        self.assertEqual(code, 404)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "unknown_container")
        self.assertFalse(console.REGISTRY.exists() and json.loads(
            console.REGISTRY.read_text() or "{}").get("accounts"))

    def test_adopt_stores_ports_from_docker_port(self):
        known = {
            "wxbot-ubuntu-manual": {
                "running": True,
                "hostname": "wechat-a",
                "volume_src": "/var/lib/docker/volumes/wxbot-ubuntu-manual-home/_data",
                "ports": "5100/tcp -> 127.0.0.1:5100\n6080/tcp -> 127.0.0.1:6082\n",
            }
        }
        with mock.patch.object(console.subprocess, "run", side_effect=self._docker(known)):
            code, payload = console.adopt("wxbot-ubuntu-manual", "  甲号  ", "  wxid_a  ")
        self.assertEqual(code, 200, payload)
        account = payload["account"]
        self.assertEqual(account["web_port"], 5100)
        self.assertEqual(account["vnc_port"], 6082)
        self.assertEqual(account["label"], "甲号")
        self.assertEqual(account["expected_wxid"], "wxid_a")
        self.assertEqual(account["hostname"], "wechat-a")
        self.assertEqual(account["volume"], "wxbot-ubuntu-manual-home")
        self.assertNotEqual(account["id"], "primary")
        self.assertEqual(account["id"], "wxbot-ubuntu-manual")
        stored = json.loads(console.REGISTRY.read_text(encoding="utf-8"))
        self.assertEqual(stored["accounts"][0]["web_port"], 5100)
        self.assertEqual(stored["accounts"][0]["vnc_port"], 6082)

    def test_sixth_adopt_is_refused(self):
        known = {}
        for i in range(6):
            name = f"wxbot-{i}"
            known[name] = {
                "running": True,
                "hostname": f"h{i}",
                "ports": f"5100/tcp -> 127.0.0.1:{5200+i}\n6080/tcp -> 127.0.0.1:{6200+i}\n",
            }
        with mock.patch.object(console.subprocess, "run", side_effect=self._docker(known)):
            for i in range(5):
                code, payload = console.adopt(f"wxbot-{i}", f"号{i}", f"wxid_{i}")
                self.assertEqual(code, 200, payload)
            code, payload = console.adopt("wxbot-5", "号5", "wxid_5")
        self.assertEqual(code, 409)
        self.assertEqual(payload["reason"], "limit_reached")
        stored = json.loads(console.REGISTRY.read_text(encoding="utf-8"))
        self.assertEqual(len(stored["accounts"]), 5)

    def test_registry_lock_file_is_the_registry_path(self):
        seen = {"ops": []}

        def flock(fd, op):
            seen["fd"] = fd
            seen["ops"].append(op)
            seen["path"] = console.REGISTRY

        known = {
            "wxbot-one": {
                "running": True,
                "ports": "5100/tcp -> 127.0.0.1:5100\n6080/tcp -> 127.0.0.1:6082\n",
            }
        }
        with mock.patch.object(console.subprocess, "run", side_effect=self._docker(known)), \
                mock.patch.object(console.fcntl, "flock", side_effect=flock):
            code, payload = console.adopt("wxbot-one", "一", "wxid_1")
        self.assertEqual(code, 200, payload)
        self.assertEqual(seen["path"], console.REGISTRY)
        self.assertIn(console.fcntl.LOCK_EX, seen["ops"])
        self.assertFalse((console.REGISTRY.parent / "registry.lock").exists())

    def test_accounts_api_has_no_stop_actions(self):
        known = {
            "wxbot-ubuntu-manual": {
                "running": True,
                "ports": "5100/tcp -> 127.0.0.1:5100\n6080/tcp -> 127.0.0.1:6082\n",
            }
        }
        with mock.patch.object(console.subprocess, "run", side_effect=self._docker(known)):
            console.adopt("wxbot-ubuntu-manual", "甲", "wxid_a")
            body = console.list_with_probes()
        text = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("stop", text)
        self.assertNotIn("restart", text)
        self.assertNotIn("start", text)
        self.assertEqual(body["accounts"][0]["container"], "wxbot-ubuntu-manual")
        self.assertTrue(body["accounts"][0]["probe"]["running"])
        page = console.PAGE
        self.assertNotIn(">停止<", page)
        self.assertNotIn(">重启<", page)
        self.assertNotIn("type=\"button\">停止", page)
        handler_src = (ROOT / "tools" / "wxbot_console.py").read_text(encoding="utf-8")
        self.assertNotIn('"/api/accounts/"', handler_src)
        self.assertNotIn("stop", [line.strip() for line in handler_src.splitlines()
                                  if "do_POST" in line or "path ==" in line])


if __name__ == "__main__":
    unittest.main()
