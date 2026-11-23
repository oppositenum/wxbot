"""Host console registry: adopt only, no start/stop, docker stubbed."""
import importlib.util
import json
import subprocess
import threading
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
        for path in (console.REGISTRY, console.REGISTRY.with_suffix(".tmp"), console.lock_path()):
            if path.exists():
                path.unlink()
        console.REGISTRY = self._reg

    def _docker(self, known):
        def run(args, timeout=None, **kwargs):
            cmd = list(args)
            self.assertEqual(cmd[0], "docker")
            if cmd[1] in ("stop", "restart", "start"):
                name = cmd[-1]
            else:
                name = next((part for part in reversed(cmd[1:]) if not str(part).startswith("-") and "{{" not in str(part)), "")
            if cmd[1] == "inspect" or str(cmd[1]).startswith("inspect"):
                if name not in known:
                    return _completed(cmd, 1, stderr="No such object: " + name)
                meta = known[name]
                joined = " ".join(cmd)
                if "Config.Env" in joined or ".Config.Env" in joined:
                    env = meta.get("env")
                    if env is None:
                        env = []
                    return _completed(cmd, 0, stdout=json.dumps(env) + "\n")
                if "binds" in meta:
                    binds = json.dumps(meta["binds"])
                else:
                    binds = json.dumps([meta.get("volume_src", "/var/lib/docker/volumes/x/_data")])
                hostname = meta.get("hostname", "wx")
                return _completed(cmd, 0, stdout=f"{binds}\t{hostname}\n")
            if cmd[1] == "port":
                if name not in known:
                    return _completed(cmd, 1, stderr="No such container")
                return _completed(cmd, 0, stdout=known[name].get("ports", ""))
            if cmd[1] in ("stop", "restart"):
                return _completed(cmd, 0, stdout=name + "\n")
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

    def test_registry_lock_is_stable_across_replace(self):
        """Two adopters must not both pass the critical section.

        The lock file is registry.json.lock. update() takes LOCK_EX before
        reading and releases it only after os.replace. A second thread blocks
        until the first replace finishes, so its read sees the first account.
        """
        known = {}
        for i in range(2):
            known[f"wxbot-{i}"] = {
                "running": True,
                "hostname": f"h{i}",
                "ports": f"5100/tcp -> 127.0.0.1:{5200+i}\n6080/tcp -> 127.0.0.1:{6200+i}\n",
            }
        entered = threading.Event()
        release = threading.Event()
        real_flock = console.fcntl.flock
        order = []

        def flock(fd, op):
            if op & console.fcntl.LOCK_UN:
                order.append("unlock")
                return real_flock(fd, op)
            order.append("lock-wait")
            if not entered.is_set():
                entered.set()
                real_flock(fd, op)
                order.append("lock-held")
                release.wait(2)
                return None
            order.append("lock-blocked")
            real_flock(fd, op)
            order.append("lock-acquired-after")
            return None

        with mock.patch.object(console.subprocess, "run", side_effect=self._docker(known)), \
                mock.patch.object(console.fcntl, "flock", side_effect=flock):
            first = threading.Thread(target=lambda: console.adopt("wxbot-0", "甲", "wxid_0"))
            first.start()
            self.assertTrue(entered.wait(2))
            second_done = threading.Event()
            second_result = {}

            def _second():
                code, payload = console.adopt("wxbot-1", "乙", "wxid_1")
                second_result["code"] = code
                second_result["payload"] = payload
                second_done.set()

            second = threading.Thread(target=_second)
            second.start()
            # Second adopt must still be blocked on the stable lock.
            self.assertFalse(second_done.wait(0.3))
            self.assertIn("lock-blocked", order)
            release.set()
            first.join(2)
            second.join(2)
        self.assertTrue(second_done.is_set())
        self.assertEqual(second_result["code"], 200)
        stored = json.loads(console.REGISTRY.read_text(encoding="utf-8"))
        self.assertEqual(
            [a["container"] for a in stored["accounts"]],
            ["wxbot-0", "wxbot-1"],
        )
        self.assertLess(order.index("lock-held"), order.index("lock-blocked"))
        self.assertLess(order.index("unlock"), order.index("lock-acquired-after"))
        self.assertTrue(console.lock_path().exists())
        self.assertEqual(console.lock_path(), Path(str(console.REGISTRY) + ".lock"))

    def test_volume_prefers_wechat_home_over_wxbot_directory(self):
        binds = [
            "/Volumes/xinba/10_Projects/Active/oppositenum/wxbot/accounts:/app/accounts",
            "/Volumes/xinba/10_Projects/Active/oppositenum/wxbot/images:/app/images",
            "wxbot-ubuntu-manual-home:/home/wechat",
        ]
        known = {
            "wxbot-ubuntu-manual": {
                "running": True,
                "hostname": "wechat-a",
                "binds": binds,
                "ports": "5100/tcp -> 127.0.0.1:5100\n6080/tcp -> 127.0.0.1:6082\n",
            }
        }
        with mock.patch.object(console.subprocess, "run", side_effect=self._docker(known)):
            code, payload = console.adopt("wxbot-ubuntu-manual", "甲", "wxid_a")
        self.assertEqual(code, 200, payload)
        self.assertEqual(payload["account"]["volume"], "wxbot-ubuntu-manual-home")
        self.assertNotEqual(payload["account"]["volume"], "wxbot")

    def test_accounts_api_has_no_stop_actions(self):
        """A live container whose self env is still 'wxbot' gets no stop control.

        Registry JSON itself has no start/stop/restart fields. The static PAGE
        template used before cards were rendered per request has no buttons.
        """
        known = {
            "wxbot-ubuntu-manual": {
                "running": True,
                "env": ["WXBOT_SELF_CONTAINER=wxbot"],
                "ports": "5100/tcp -> 127.0.0.1:5100\n6080/tcp -> 127.0.0.1:6082\n",
            }
        }
        with mock.patch.object(console.subprocess, "run", side_effect=self._docker(known)):
            console.adopt("wxbot-ubuntu-manual", "甲", "wxid_a")
            body = console.list_with_probes("127.0.0.1:5090")
            page = console.render_page("127.0.0.1:5090")
        text = json.dumps({k: v for k, v in body["accounts"][0].items() if k != "can_stop"}, ensure_ascii=False)
        self.assertNotIn("stop", text)
        self.assertNotIn("restart", text)
        self.assertNotIn("start", text)
        self.assertFalse(body["accounts"][0]["can_stop"])
        self.assertEqual(body["accounts"][0]["container"], "wxbot-ubuntu-manual")
        self.assertTrue(body["accounts"][0]["probe"]["running"])
        self.assertNotIn(">停止<", page)
        self.assertNotIn(">重启<", page)
        self.assertNotIn('type="submit">停止', page)

    def test_card_links_use_page_host_and_registered_ports(self):
        known = {
            "wxbot-ubuntu-manual": {
                "running": True,
                "env": ["WXBOT_SELF_CONTAINER=wxbot"],
                "ports": "5100/tcp -> 127.0.0.1:5100\n6080/tcp -> 127.0.0.1:6082\n",
            }
        }
        with mock.patch.object(console.subprocess, "run", side_effect=self._docker(known)):
            console.adopt("wxbot-ubuntu-manual", "甲", "wxid_a")
            body = console.list_with_probes("127.0.0.1:5090")
            page = console.render_page("127.0.0.1:5090")
        account = body["accounts"][0]
        self.assertEqual(account["admin_url"], "http://127.0.0.1:5100/")
        self.assertEqual(
            account["vnc_url"],
            "http://127.0.0.1:6082/vnc.html?autoconnect=true&resize=scale&view_only=false&path=websockify",
        )
        self.assertIn("http://127.0.0.1:5100/", page)
        self.assertIn(
            "http://127.0.0.1:6082/vnc.html?autoconnect=true&amp;resize=scale&amp;view_only=false&amp;path=websockify",
            page,
        )
        self.assertNotIn("iframe", page.lower())
        v6 = console.render_page("[2001:db8::1]:5090")
        self.assertIn("http://[2001:db8::1]:5100/", v6)
        self.assertIn("http://[2001:db8::1]:6082/vnc.html", v6)
        self.assertEqual(console.page_hostname("0.0.0.0:5090"), "")
        self.assertEqual(console.page_hostname(""), "")

    def test_missing_port_omits_that_link(self):
        account = {"container": "wxbot-a", "web_port": 5101, "vnc_port": None, "id": "wxbot-a"}
        admin, vnc = console.entry_urls(account, "127.0.0.1:5090")
        self.assertEqual(admin, "http://127.0.0.1:5101/")
        self.assertEqual(vnc, "")
        self.assertNotIn("6080", admin)

    def test_self_mismatch_has_no_stop_and_confirm_mismatch_does_not_stop(self):
        calls = []
        known = {
            "wxbot-ubuntu-manual": {
                "running": True,
                "env": ["WXBOT_SELF_CONTAINER=wxbot"],
                "ports": "5100/tcp -> 127.0.0.1:5100\n6080/tcp -> 127.0.0.1:6082\n",
            },
            "wxbot-second": {
                "running": True,
                "env": ["WXBOT_SELF_CONTAINER=wxbot-second"],
                "ports": "5100/tcp -> 127.0.0.1:5101\n6080/tcp -> 127.0.0.1:6083\n",
            },
        }
        real = self._docker(known)

        def run(args, timeout=None, **kwargs):
            cmd = list(args)
            if cmd[1] in ("stop", "restart"):
                calls.append(cmd)
            return real(args, timeout=timeout, **kwargs)

        with mock.patch.object(console.subprocess, "run", side_effect=run):
            console.adopt("wxbot-ubuntu-manual", "甲", "wxid_a")
            console.adopt("wxbot-second", "乙", "wxid_b")
            page = console.render_page("127.0.0.1:5090")
            mismatch = console.account_control("wxbot-second", "stop", "wxid_other")
            matched = console.account_control("wxbot-second", "stop", "wxid_b")
            refused = console.account_control("wxbot-ubuntu-manual", "stop", "wxid_a")
        self.assertNotIn(">停止<", page.split("wxbot-ubuntu-manual")[1].split("</section>")[0])
        self.assertIn(">停止<", page)
        self.assertEqual(mismatch[0], 400)
        self.assertEqual(mismatch[1]["reason"], "confirm_wxid_mismatch")
        self.assertEqual(calls, [["docker", "stop", "wxbot-second"]])
        self.assertEqual(matched[0], 200)
        self.assertEqual(refused[0], 403)
        self.assertEqual(refused[1]["reason"], "self_name_mismatch")


if __name__ == "__main__":
    unittest.main()
