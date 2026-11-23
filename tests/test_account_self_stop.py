"""In-container account stop may target only this process's own container."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class OwnContainerStopTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def _post(self, path, env, body=None):
        with patch.dict(os.environ, env, clear=False):
            return self.client.post(
                path,
                json=body or {},
                base_url="http://127.0.0.1:5100",
                headers={"X-Wxbot-Local-Admin": "1"},
            )

    def test_stop_other_container_is_403_even_when_local(self):
        env = {"WXBOT_LOCAL": "1", "WXBOT_SELF_CONTAINER": "wxbot-ubuntu-manual"}
        with patch("core.multi_account.action") as action:
            response = self._post("/api/accounts/wxbot-second/stop", env)
        self.assertEqual(response.status_code, 403)
        action.assert_not_called()

    def test_unset_self_container_refuses_stop(self):
        env = {"WXBOT_LOCAL": "1", "WXBOT_SELF_CONTAINER": ""}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("WXBOT_SELF_CONTAINER", None)
            with patch("core.multi_account.action") as action:
                response = self.client.post(
                    "/api/accounts/wxbot-ubuntu-manual/stop",
                    json={},
                    base_url="http://127.0.0.1:5100",
                )
                batch = self.client.post(
                    "/api/accounts/batch",
                    json={"action": "restart"},
                    base_url="http://127.0.0.1:5100",
                )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(batch.status_code, 403)
        action.assert_not_called()

    def test_batch_stop_of_another_account_is_403(self):
        env = {"WXBOT_LOCAL": "1", "WXBOT_SELF_CONTAINER": "wxbot-ubuntu-manual"}
        accounts = [
            {"id": "wxbot-ubuntu-manual"},
            {"id": "wxbot-second"},
        ]
        with patch.dict(os.environ, env, clear=False), \
                patch("core.multi_account.list_accounts", return_value=accounts), \
                patch("core.multi_account.action") as action:
            response = self.client.post(
                "/api/accounts/batch",
                json={"action": "stop"},
                base_url="http://127.0.0.1:5100",
            )
        self.assertEqual(response.status_code, 403)
        action.assert_not_called()

    def test_stop_own_container_reaches_action(self):
        env = {"WXBOT_LOCAL": "1", "WXBOT_SELF_CONTAINER": "wxbot-ubuntu-manual"}
        with patch.dict(os.environ, env, clear=False), \
                patch("core.multi_account.action", return_value={"ok": True}) as action:
            response = self.client.post(
                "/api/accounts/wxbot-ubuntu-manual/restart",
                json={},
                base_url="http://127.0.0.1:5100",
            )
        self.assertEqual(response.status_code, 200)
        action.assert_called_once_with("wxbot-ubuntu-manual", "restart")


if __name__ == "__main__":
    unittest.main()
