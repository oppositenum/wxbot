"""Manual management must never route old tabs into account reads or sends."""
import os
import subprocess
import unittest
from unittest.mock import patch

from core.desktop_management import DEFAULT_PROFILES, create_app, desktop_url, probe


class DesktopManagement(unittest.TestCase):
    def setUp(self):
        self.client = create_app().test_client()

    def _url(self, host, port):
        env = {"WXBOT_NOVNC_PORT": str(port), "WXBOT_PUBLIC_NOVNC_URL": ""}
        with patch.dict(os.environ, env, clear=False):
            return self.client.get("/api/desktop/instances", headers={"Host": host}).json

    def test_published_port_uses_request_host(self):
        cases = [
            ("127.0.0.1:5100", "6082", "http://127.0.0.1:6082/"),
            ("localhost:5101", "6083", "http://localhost:6083/"),
            ("[::1]:5100", "6082", "http://[::1]:6082/"),
        ]
        for host, port, prefix in cases:
            data = self._url(host, port)
            self.assertEqual(len(data["instances"]), 1, host)
            self.assertEqual(data["instances"][0]["port"], int(port))
            self.assertTrue(data["instances"][0]["desktop_url"].startswith(prefix), host)
            with patch.dict(os.environ, {"WXBOT_NOVNC_PORT": port, "WXBOT_PUBLIC_NOVNC_URL": ""}, clear=False):
                login = self.client.post("/api/login", headers={"Host": host}).json
            self.assertTrue(login["novnc_url"].startswith(prefix), host)

    def test_unset_port_embeds_nothing(self):
        env = {"WXBOT_NOVNC_PORT": "", "WXBOT_PUBLIC_NOVNC_URL": ""}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("WXBOT_NOVNC_PORT", None)
            data = self.client.get("/api/desktop/instances", headers={"Host": "127.0.0.1:5100"}).json
            self.assertEqual(data["instances"][0]["desktop_url"], "")
            self.assertEqual(desktop_url(DEFAULT_PROFILES["primary"]), "")
            self.assertNotIn("port", data["instances"][0])

    def test_instance_query_does_not_switch_profiles(self):
        with patch.dict(os.environ, {"WXBOT_NOVNC_PORT": "6082", "WXBOT_PUBLIC_NOVNC_URL": ""}, clear=False):
            chosen = self.client.get("/api/desktop/status?instance=other-account",
                                     headers={"Host": "127.0.0.1:5100"}).json
        self.assertEqual(chosen["id"], "primary")
        self.assertEqual(chosen["container"], "wxbot-ubuntu-manual")
        self.assertIn("127.0.0.1:6082", chosen["desktop_url"])
        self.assertNotIn("/vnc/vnc.html", chosen["desktop_url"])

    def test_stale_tabs_cannot_read_old_accounts_or_control_automation(self):
        for method, path in [('post','/api/send'), ('post','/api/bot/start'),
                             ('post','/api/sync'), ('post','/api/keys/capture_img'),
                             ('post','/api/schedule/1/run'), ('post','/api/upload'),
                             ('get','/api/messages'), ('get','/api/contacts'),
                             ('get','/api/sessions'), ('get','/api/personalization')]:
            response = getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 409, path)
            self.assertEqual(response.json['code'], 'desktop_only')
        self.assertFalse(self.client.get('/api/bot').json['running'])

    @patch('core.desktop_management.subprocess.run')
    def test_untrusted_instance_never_reaches_docker(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, '', '')
        for path in ['/api/desktop/status', '/api/status']:
            response = self.client.get(path+'?instance=other-container')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['container'], 'wxbot-ubuntu-manual')
        for call in run.call_args_list:
            self.assertIn('wxbot-ubuntu-manual', call.args[0])
            self.assertNotIn('other-container', call.args[0])

    @patch('core.desktop_management.subprocess.run')
    def test_process_does_not_claim_authenticated_login(self, run):
        run.side_effect = [subprocess.CompletedProcess([],0,'true\n',''),
                           subprocess.CompletedProcess([],0,'62\n','')]
        state = self.client.get('/api/status').json
        self.assertTrue(state['wechat_running'])
        self.assertIsNone(state['logged_in'])
        self.assertEqual(state['login_state'], 'unknown')
        self.assertTrue(all('wxbot-ubuntu-manual' in c.args[0] for c in run.call_args_list))
        self.assertIn('/opt/wechat/wechat', run.call_args_list[1].args[0])
        self.assertNotIn('-x', run.call_args_list[1].args[0])

    @patch('core.desktop_management.subprocess.run', side_effect=subprocess.TimeoutExpired('docker',4))
    def test_timeout_is_reported_without_false_online_state(self, run):
        state = probe(DEFAULT_PROFILES['primary'])
        self.assertFalse(state['wechat_running'])
        self.assertIsNotNone(state['error'])


if __name__ == '__main__':
    unittest.main()
