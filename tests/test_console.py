"""控制台(console/server.py)。需要 aiohttp;没装时跳过(镜像里有)。"""
import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import aiohttp  # noqa: F401
    from aiohttp.test_utils import TestClient, TestServer
    HAVE_AIOHTTP = True
except ImportError:  # pragma: no cover
    HAVE_AIOHTTP = False

ROOT = Path(__file__).resolve().parents[1]


def load(tmp):
    with patch.dict(os.environ, {"CONSOLE_DATA": tmp, "WXBOT_UI_USER": "admin", "WXBOT_UI_PASSWORD": "pw",
                                 "WXBOT_SECRET": "s3cret", "HOSTNAME": "selfid", "VNC_PASSWORD": "vnc"}):
        sys.path.insert(0, str(ROOT / "console"))
        try:
            import server as console
            return importlib.reload(console)
        finally:
            sys.path.pop(0)


class FakeDocker:
    def __init__(self, containers):
        self.cs = containers            # name -> inspect dict
        self.created, self.deleted, self.connected, self.actions = [], [], [], []

    async def containers(self):
        return [{"Names": ["/" + n]} for n in self.cs]

    async def inspect(self, name):
        if name == "selfid":
            return {"Name": "/wxbot-console", "Config": {"Image": "img:self"}}
        if name not in self.cs:
            raise self.err(404, "No such container")
        return self.cs[name]

    async def ensure_network(self):
        pass

    async def connect(self, name):
        self.connected.append(name)

    async def action(self, name, op):
        self.actions.append((name, op))

    async def req(self, method, path, body=None, params=None, ok=None):
        if method == "POST" and path == "/containers/create":
            self.created.append((params["name"], body))
            self.cs[params["name"]] = {"Config": {"ExposedPorts": {"5100/tcp": {}}, "Labels": body["Labels"]},
                                       "State": {"Status": "running"}}
            return {"Id": "x"}
        if method == "DELETE":
            self.deleted.append(path)
            return ""
        return {}


def web_container(**labels):
    return {"Config": {"ExposedPorts": {"5100/tcp": {}, "6080/tcp": {}}, "Labels": labels}, "State": {"Status": "running"}}


@unittest.skipUnless(HAVE_AIOHTTP, "需要 aiohttp")
class ConsoleTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = load(self.tmp.name)
        self.c.FAIL_SLEEP = 0
        self.fd = FakeDocker({"wxbot-ubuntu-manual": web_container(), "wxbot-second": web_container(),
                              "wxbot-second-old-4118": web_container(), "wxbot-console": web_container(),
                              "postgres-dev": web_container(), "wxbot-tool": {"Config": {}, "State": {}}})
        self.fd.err = self.c.DockerError
        self.c.docker = self.fd

    def tearDown(self):
        self.tmp.cleanup()

    def test_token_sign_verify_tamper_expiry(self):
        t = self.c._sign({"u": "admin", "exp": time.time() + 60})
        self.assertNotIn("=", t)
        self.assertEqual(self.c._verify(t)["u"], "admin")
        self.assertIsNone(self.c._verify(t[:-1] + ("0" if t[-1] != "0" else "1")))
        self.assertIsNone(self.c._verify(self.c._sign({"u": "admin", "exp": time.time() - 1})))

    def test_inject_rewrites_absolute_paths_and_adds_shim(self):
        out = self.c._inject('<html><head><title>x</title></head><body><a href="/moments">m</a>'
                             '<img src="/api/avatar?u=1"><a href="//cdn/x">c</a><a href="/a/w1/x">k</a></body></html>',
                             "/a/w2")
        self.assertIn('href="/a/w2/moments"', out)
        self.assertIn('src="/a/w2/api/avatar?u=1"', out)
        self.assertIn('href="//cdn/x"', out)
        self.assertIn('href="/a/w1/x"', out)
        self.assertIn('window.__wxconsole=P', out)
        self.assertIn('/ui/legacy.css', out)

    async def test_adopt_skips_console_backups_and_ignored(self):
        n = await self.c.adopt()
        self.assertEqual(n, 2)
        names = [a["container"] for a in self.c.load_registry()["accounts"]]
        self.assertEqual(sorted(names), ["wxbot-second", "wxbot-ubuntu-manual"])
        self.assertIn("wxbot-ubuntu-manual", self.fd.connected)
        reg = self.c.load_registry()
        reg["accounts"] = [a for a in reg["accounts"] if a["container"] != "wxbot-second"]
        reg["ignored"] = ["wxbot-second"]
        self.c.save_registry(reg)
        self.assertEqual(await self.c.adopt(), 0)                         # 启动时不再纳入
        self.assertEqual(await self.c.adopt(include_ignored=True), 1)     # 手动扫描会

    async def client(self):
        cl = TestClient(TestServer(self.c.make_app()))
        await cl.start_server()
        self.addAsyncCleanup(cl.close)
        return cl

    async def test_requires_login(self):
        cl = await self.client()
        r = await cl.get("/api/console/accounts")
        self.assertEqual(r.status, 401)
        r = await cl.get("/", allow_redirects=False)
        self.assertEqual((r.status, r.headers["Location"]), (302, "/ui/login.html"))
        r = await cl.post("/api/console/login", json={"username": "admin", "password": "bad"})
        self.assertEqual(r.status, 401)
        r = await cl.post("/api/console/login", json={"username": "admin", "password": "pw"})
        self.assertEqual(r.status, 200)
        r = await cl.get("/api/console/auth")
        self.assertTrue((await r.json())["ok"])

    async def test_login_lockout_and_no_username_leak(self):
        cl = await self.client()
        d = await (await cl.get("/api/console/auth")).json()
        self.assertEqual((d["ok"], d["user"]), (False, None))
        h = {"X-Real-IP": "1.2.3.4"}
        for _ in range(5):
            r = await cl.post("/api/console/login", json={"username": "admin", "password": "bad"}, headers=h)
            self.assertEqual(r.status, 401)
        r = await cl.post("/api/console/login", json={"username": "admin", "password": "pw"}, headers=h)
        self.assertEqual(r.status, 429)                                   # 锁住了,密码对也不行
        r = await cl.post("/api/console/login", json={"username": "admin", "password": "pw"},
                          headers={"X-Real-IP": "5.6.7.8", "X-Forwarded-Proto": "https"})
        self.assertEqual(r.status, 200)                                   # 别的 IP 不受影响
        self.assertIn("Secure", r.headers["Set-Cookie"])
        for _ in range(6):                                                 # 没有真实 IP:不锁(否则谁都能把你锁在外面)
            r = await cl.post("/api/console/login", json={"username": "admin", "password": "bad"})
            self.assertEqual(r.status, 401)
        r = await cl.post("/api/console/login", json={"username": "admin", "password": "pw"})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["X-Frame-Options"], "SAMEORIGIN")

    async def test_create_and_delete(self):
        cl = await self.client()
        await cl.post("/api/console/login", json={"username": "admin", "password": "pw"})
        r = await cl.post("/api/console/accounts", json={"label": "客服号"})
        d = await r.json()
        self.assertTrue(d["ok"], d)
        name, body = self.fd.created[0]
        self.assertEqual(name, "wxbot-acc1")
        env = dict(e.split("=", 1) for e in body["Env"])
        self.assertEqual((env["WXBOT_SEND_MODE"], env["WXBOT_SELF_CONTAINER"], env["WXBOT_UI_USER"]),
                         ("direct", "wxbot-acc1", "admin"))
        self.assertNotEqual(env["WXBOT_SECRET"], "s3cret")                 # 每个号自己的签名密钥
        self.assertEqual(body["Image"], "img:self")
        self.assertEqual([m["Source"] for m in body["HostConfig"]["Mounts"]], ["wxbot-acc1-home", "wxbot-acc1-data"])
        self.assertEqual(body["HostConfig"]["NetworkMode"], "wxbot-net")
        self.assertTrue(body["NetworkingConfig"]["EndpointsConfig"]["wxbot-net"]["MacAddress"].startswith("00:1b:21:"))
        self.assertIn(("wxbot-acc1", "start"), self.fd.actions)
        r = await cl.delete(f"/api/console/accounts/{d['id']}?purge=1")
        self.assertTrue((await r.json())["ok"])
        self.assertIn("/containers/wxbot-acc1", self.fd.deleted)
        self.assertIn("/volumes/wxbot-acc1-home", self.fd.deleted)

    async def test_note_is_optional_and_can_be_cleared(self):
        await self.c.adopt()
        self.assertEqual({a["label"] for a in self.c.load_registry()["accounts"]}, {""})   # 纳入时不起名,显示昵称
        cl = await self.client()
        await cl.post("/api/console/login", json={"username": "admin", "password": "pw"})
        d = await (await cl.post("/api/console/accounts", json={})).json()
        self.assertTrue(d["ok"], d)
        for v in ("客服号", ""):
            r = await cl.patch(f"/api/console/accounts/{d['id']}", json={"label": v})
            self.assertTrue((await r.json())["ok"])
            self.assertEqual([a["label"] for a in self.c.load_registry()["accounts"] if a["id"] == d["id"]], [v])

    async def test_removing_adopted_container_only_unregisters(self):
        await self.c.adopt()
        cl = await self.client()
        await cl.post("/api/console/login", json={"username": "admin", "password": "pw"})
        acct = [a for a in self.c.load_registry()["accounts"] if a["container"] == "wxbot-second"][0]
        r = await cl.delete(f"/api/console/accounts/{acct['id']}?purge=1")
        self.assertTrue((await r.json())["ok"])
        self.assertEqual(self.fd.deleted, [])
        self.assertIn("wxbot-second", self.c.load_registry()["ignored"])


if __name__ == "__main__":
    unittest.main()
