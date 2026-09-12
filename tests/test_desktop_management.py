"""Manual management must never route old tabs into account reads or sends."""
import subprocess
import unittest
from unittest.mock import patch

from core.desktop_management import create_app, probe, PROFILES


class DesktopManagement(unittest.TestCase):
    def setUp(self):
        self.client = create_app().test_client()

    def test_default_is_ubuntu_and_profiles_are_separate(self):
        data = self.client.get('/api/desktop/instances').json
        self.assertEqual(data['default_instance'], 'ubuntu')
        self.assertEqual([p['port'] for p in data['instances']], [6082, 6080])
        self.assertIn(':6082/', self.client.post('/api/login').json['novnc_url'])
        self.assertIn(':6080/', self.client.post('/api/login?instance=legacy').json['novnc_url'])
        self.assertIn(':6082/', self.client.post('/api/login').json['novnc_url'])

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
        for path in ['/api/desktop/status', '/api/status']:
            self.assertEqual(self.client.get(path+'?instance=other-container').status_code, 400)
        run.assert_not_called()

    @patch('core.desktop_management.subprocess.run')
    def test_process_does_not_claim_authenticated_login(self, run):
        run.side_effect = [subprocess.CompletedProcess([],0,'true\n',''),
                           subprocess.CompletedProcess([],0,'62\n','')]
        state = self.client.get('/api/status').json
        self.assertTrue(state['wechat_running'])
        self.assertIsNone(state['logged_in'])
        self.assertEqual(state['login_state'], 'unknown')
        self.assertTrue(all('wxbot-ubuntu-manual' in c.args[0] for c in run.call_args_list))

    @patch('core.desktop_management.subprocess.run', side_effect=subprocess.TimeoutExpired('docker',4))
    def test_timeout_is_reported_without_false_online_state(self, run):
        state = probe(PROFILES['ubuntu'])
        self.assertFalse(state['wechat_running'])
        self.assertIsNotNone(state['error'])


if __name__ == '__main__':
    unittest.main()
