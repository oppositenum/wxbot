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

    def test_send_targets_require_login_and_shape_the_copy_list(self):
        anon = self.client.get("/api/send/targets")
        self.assertEqual(anon.status_code, 401)
        self.client.post("/api/auth/login", json={
            "username": "test-user", "password": "test-password"})
        people = [{"name": "叮", "username": "wxid_a"}]
        groups = [{"name": "南风闲话群", "username": "1@chatroom"}]
        with patch.dict(os.environ, {"WXBOT_API_TOKEN": "local-token"}, clear=False), \
             patch("core.contacts.list_contacts", return_value=people), \
             patch("core.contacts.list_groups", return_value=groups):
            r = self.client.get("/api/send/targets")
        self.assertEqual(r.status_code, 200)
        body = r.json
        self.assertEqual(body["token"], "local-token")
        self.assertEqual(body["url"], "http://localhost/api/send")
        self.assertEqual(body["targets"], [
            {"kind": "私聊", "to": "叮", "chat": "wxid_a"},
            {"kind": "群", "to": "南风闲话群", "chat": "1@chatroom"},
        ])

    def test_anonymous_api_is_unauthorized(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 401)

    def test_api_token_opens_send_and_push_only(self):
        with patch.dict(os.environ, {"WXBOT_API_TOKEN": "local-token"}, clear=False):
            denied = self.client.get("/api/status", headers={"X-Wxbot-Token": "local-token"})
            self.assertEqual(denied.status_code, 401)
            wrong = self.client.post("/api/send", headers={"X-Wxbot-Token": "nope"},
                                     json={"to": "群", "type": "text", "content": "hi"})
            self.assertEqual(wrong.status_code, 401)
            ok = self.client.post("/api/send", headers={"X-Wxbot-Token": "local-token"},
                                  json={"type": "text", "content": "hi"})
            self.assertEqual(ok.status_code, 400)
            body = self.client.post("/api/send", json={
                "token": "local-token", "type": "text", "content": "hi"})
            self.assertEqual(body.status_code, 400)
            bad_body = self.client.post("/api/send", json={
                "token": "nope", "to": "群", "type": "text", "content": "hi"})
            self.assertEqual(bad_body.status_code, 401)
            push = self.client.get("/api/bot/push", headers={"X-Wxbot-Token": "local-token"})
            self.assertEqual(push.status_code, 200)

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
