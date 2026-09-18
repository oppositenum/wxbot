"""5100 管理页账号密码门。不连微信。"""
import os
import unittest
from unittest.mock import patch


class UiLogin(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "WXBOT_UI_AUTH": "1",
            "WXBOT_UI_USER": "xinba",
            "WXBOT_UI_PASSWORD": "123",
            "WXBOT_SECRET": "test-secret",
        }, clear=False)
        self.env.start()
        import server
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def tearDown(self):
        self.env.stop()

    def test_anonymous_html_redirects_to_login(self):
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/login"))

    def test_anonymous_api_is_unauthorized(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 401)

    def test_wrong_password_rejected(self):
        r = self.client.post("/api/auth/login", json={"username": "xinba", "password": "wrong"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.client.get("/api/status").status_code, 401)

    def test_login_then_status_ok(self):
        r = self.client.post("/api/auth/login", json={"username": "xinba", "password": "123"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json["ok"])
        self.assertEqual(self.client.get("/api/status").status_code, 200)

    def test_logout_locks_again(self):
        self.client.post("/api/auth/login", json={"username": "xinba", "password": "123"})
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/status").status_code, 401)

    def test_idle_timeout_returns_to_login(self):
        import time
        with patch.dict(os.environ, {"WXBOT_UI_IDLE": "600"}, clear=False):
            self.client.post("/api/auth/login", json={"username": "xinba", "password": "123"})
            self.assertEqual(self.client.get("/api/status").status_code, 200)
            with self.client.session_transaction() as sess:
                sess["ui_seen"] = int(time.time()) - 601
            self.assertEqual(self.client.get("/api/status").status_code, 401)
            r = self.client.get("/", follow_redirects=False)
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.headers["Location"].endswith("/login"))

    def test_polling_does_not_extend_idle(self):
        import time
        self.client.post("/api/auth/login", json={"username": "xinba", "password": "123"})
        with self.client.session_transaction() as sess:
            seen = int(time.time()) - 30
            sess["ui_seen"] = seen
        self.assertEqual(self.client.get("/api/status").status_code, 200)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("ui_seen"), seen)
        self.assertEqual(self.client.post("/api/auth/touch").status_code, 200)
        with self.client.session_transaction() as sess:
            self.assertGreaterEqual(sess.get("ui_seen"), seen + 29)


if __name__ == "__main__":
    unittest.main()
