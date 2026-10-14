"""5100 管理页账号密码门。不连微信。"""
import os
import unittest
from unittest.mock import patch


class UiLogin(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "WXBOT_UI_AUTH": "1",
            "WXBOT_UI_USER": "test-user",
            "WXBOT_UI_PASSWORD": "test-password",
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
        r = self.client.post("/api/auth/login", json={"username": "test-user", "password": "wrong"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.client.get("/api/status").status_code, 401)

    def test_missing_env_credentials_are_not_replaced_by_code_defaults(self):
        with patch.dict(os.environ, {"WXBOT_UI_USER": "", "WXBOT_UI_PASSWORD": ""}, clear=False):
            r = self.client.post("/api/auth/login", json={"username": "test-user", "password": "test-password"})
        self.assertEqual(r.status_code, 503)

    def test_login_then_status_ok(self):
        r = self.client.post("/api/auth/login", json={"username": "test-user", "password": "test-password"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json["ok"])
        self.assertEqual(self.client.get("/api/status").status_code, 200)

    def test_logout_locks_again(self):
        self.client.post("/api/auth/login", json={"username": "test-user", "password": "test-password"})
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/status").status_code, 401)

    def test_idle_timeout_returns_to_login(self):
        import time
        with patch.dict(os.environ, {"WXBOT_UI_IDLE": "600"}, clear=False):
            self.client.post("/api/auth/login", json={"username": "test-user", "password": "test-password"})
            self.assertEqual(self.client.get("/api/status").status_code, 200)
            with self.client.session_transaction() as sess:
                sess["ui_seen"] = int(time.time()) - 601
            self.assertEqual(self.client.get("/api/status").status_code, 401)
            r = self.client.get("/", follow_redirects=False)
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.headers["Location"].endswith("/login"))

    def test_polling_does_not_extend_idle(self):
        import time
        self.client.post("/api/auth/login", json={"username": "test-user", "password": "test-password"})
        with self.client.session_transaction() as sess:
            seen = int(time.time()) - 30
            sess["ui_seen"] = seen
        self.assertEqual(self.client.get("/api/status").status_code, 200)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("ui_seen"), seen)
        self.assertEqual(self.client.post("/api/auth/touch").status_code, 200)
        with self.client.session_transaction() as sess:
            self.assertGreaterEqual(sess.get("ui_seen"), seen + 29)

    def test_session_status_expires_without_user_activity(self):
        import time
        self.client.post('/api/auth/login', json={
            'username': 'test-user', 'password': 'test-password'})
        with self.client.session_transaction() as sess:
            seen = int(time.time())
            sess['ui_seen'] = seen
        with patch.dict(os.environ, {'WXBOT_UI_IDLE': '30'}):
            with patch('server.time.time', return_value=seen + 29):
                response = self.client.get('/api/auth/status')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertNotIn('Set-Cookie', response.headers)
            with self.client.session_transaction() as sess:
                self.assertEqual(sess['ui_seen'], seen)
            with patch('server.time.time', return_value=seen + 31):
                self.assertEqual(self.client.get('/api/auth/status').status_code, 401)


if __name__ == "__main__":
    unittest.main()
