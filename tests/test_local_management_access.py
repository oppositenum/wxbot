"""Request-bound local-owner access tests; no data reads, models or sends."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server

class Access(unittest.TestCase):
    def allowed(self, *, enabled=True, host='http://127.0.0.1:5100', peer='127.0.0.1', headers=None):
        h={'X-Wxbot-Local-Admin':'1'} if headers is None else headers
        with patch.dict(os.environ,{'WXBOT_LOCAL_ADMIN':'1' if enabled else '0'}),server.app.test_request_context('/api/personalization',base_url=host,headers=h,environ_base={'REMOTE_ADDR':peer}):
            return server.local_management_access()
    def test_local_same_origin_without_secret(self):
        for host in ('http://127.0.0.1:5100','http://localhost:5100','http://[::1]:5100'):
            self.assertTrue(self.allowed(host=host,headers={'X-Wxbot-Local-Admin':'1','Origin':host,'Sec-Fetch-Site':'same-origin'}))
    def test_not_enabled_by_default(self):self.assertFalse(self.allowed(enabled=False))
    def test_remote_peer_denied(self):self.assertFalse(self.allowed(peer='192.0.2.5'))
    def test_rebound_or_other_port_denied(self):
        for host in ('http://evil.example:5100','http://localhost:5188','http://127.0.0.1.evil.example:5100'):
            self.assertFalse(self.allowed(host=host))
    def test_custom_header_required(self):self.assertFalse(self.allowed(headers={}))
    def test_cross_site_denied(self):
        for extra in ({'Origin':'https://evil.example'},{'Origin':'null'},{'Sec-Fetch-Site':'cross-site'},{'Sec-Fetch-Site':'same-site'}):
            self.assertFalse(self.allowed(headers=dict({'X-Wxbot-Local-Admin':'1'},**extra)))
    def test_forwarded_requests_denied(self):
        for key in ('Forwarded','X-Forwarded-For','X-Forwarded-Host','X-Forwarded-Proto'):
            self.assertFalse(self.allowed(headers={'X-Wxbot-Local-Admin':'1',key:'127.0.0.1'}))
    def test_existing_token_and_local_access_are_distinct(self):
        with patch.dict(os.environ,{'WXBOT_LOCAL_ADMIN':'1','WXBOT_ADMIN_READ_TOKEN':'unit-test-token'}),patch('config.account_key',return_value='unit-account'):
            with server.app.test_request_context('/api/personalization',base_url='http://127.0.0.1:5100',headers={'X-Wxbot-Local-Admin':'1'},environ_base={'REMOTE_ADDR':'127.0.0.1'}):
                self.assertIsNone(server.authorize_private_management())
            with server.app.test_request_context('/api/personalization',headers={'X-Wxbot-Admin-Token':'unit-test-token'},environ_base={'REMOTE_ADDR':'192.0.2.5'}):
                self.assertIsNone(server.authorize_private_management())
            with server.app.test_request_context('/api/personalization',environ_base={'REMOTE_ADDR':'192.0.2.5'}):
                self.assertEqual(server.authorize_private_management()[1],403)
    def test_access_endpoint_exposes_no_credential(self):
        with patch.dict(os.environ,{'WXBOT_LOCAL_ADMIN':'1'}):
            response=server.app.test_client().get('/api/admin/access',base_url='http://127.0.0.1:5100',headers={'X-Wxbot-Local-Admin':'1'})
            self.assertEqual(response.json,{'local_admin':True})
if __name__=='__main__':unittest.main()
