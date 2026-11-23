"""In-container account stop may target only this process's own container."""
import json
import os
import subprocess
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from core import multi_account


class OwnContainerStopTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.registry = Path(__file__).resolve().parent / (self.id().replace(".", "_") + ".json")
        self._saved = multi_account.REGISTRY
        multi_account.REGISTRY = self.registry
        if self.registry.exists():
            self.registry.unlink()

    def tearDown(self):
        multi_account.REGISTRY = self._saved
        for path in (self.registry, self.registry.with_suffix(".tmp")):
            if path.exists():
                path.unlink()

    def _write(self, accounts):
        self.registry.write_text(json.dumps({"accounts": accounts}), encoding="utf-8")

    def _post(self, path, env, body=None):
        with patch.dict(os.environ, env, clear=False):
            return self.client.post(
                path,
                json=body or {},
                base_url="http://127.0.0.1:5100",
                headers={"X-Wxbot-Local-Admin": "1"},
            )

    def test_stop_other_container_is_403_even_when_local(self):
        self._write([{"id": "wxbot-second", "container": "wxbot-second"}])
        env = {"WXBOT_LOCAL": "1", "WXBOT_SELF_CONTAINER": "wxbot-ubuntu-manual"}
        with patch("core.multi_account.subprocess.run") as run:
            response = self._post("/api/accounts/wxbot-second/stop", env)
        self.assertEqual(response.status_code, 403)
        run.assert_not_called()

    def test_id_matching_self_does_not_stop_a_different_container(self):
        self._write([{"id": "wxbot-ubuntu-manual", "container": "wxbot-second"}])
        env = {"WXBOT_LOCAL": "1", "WXBOT_SELF_CONTAINER": "wxbot-ubuntu-manual"}
        with patch("core.multi_account.subprocess.run") as run:
            response = self._post("/api/accounts/wxbot-ubuntu-manual/stop", env)
        self.assertEqual(response.status_code, 403)
        run.assert_not_called()

    def test_primary_row_stops_its_container_not_the_id(self):
        self._write([{"id": "primary", "container": "wxbot-ubuntu-manual"}])
        env = {"WXBOT_LOCAL": "1", "WXBOT_SELF_CONTAINER": "wxbot-ubuntu-manual"}
        stopped = []

        def run(args, **kwargs):
            stopped.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch("core.multi_account.shutil.which", return_value="/usr/bin/docker"), \
                patch("core.multi_account.subprocess.run", side_effect=run), \
                patch("core.multi_account._status", return_value={"container": "running", "running": True, "online": False}):
            response = self._post("/api/accounts/primary/stop", env)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(stopped, [["docker", "stop", "wxbot-ubuntu-manual"]])

    def test_unset_self_container_refuses_stop(self):
        self._write([{"id": "primary", "container": "wxbot-ubuntu-manual"}])
        with patch.dict(os.environ, {"WXBOT_LOCAL": "1"}, clear=False):
            os.environ.pop("WXBOT_SELF_CONTAINER", None)
            with patch("core.multi_account.subprocess.run") as run:
                response = self.client.post(
                    "/api/accounts/primary/stop",
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
        for call in run.call_args_list:
            self.assertNotIn("stop", call.args[0])
            self.assertNotIn("restart", call.args[0])

    def test_batch_stop_of_another_account_is_403(self):
        self._write([
            {"id": "primary", "container": "wxbot-ubuntu-manual"},
            {"id": "second", "container": "wxbot-second"},
        ])
        env = {"WXBOT_LOCAL": "1", "WXBOT_SELF_CONTAINER": "wxbot-ubuntu-manual"}
        with patch("core.multi_account.subprocess.run") as run:
            response = self._post("/api/accounts/batch", env, {"action": "stop"})
        self.assertEqual(response.status_code, 403)
        for call in run.call_args_list:
            self.assertNotIn("stop", call.args[0])
            self.assertNotIn("restart", call.args[0])

    def test_missing_row_is_403(self):
        env = {"WXBOT_LOCAL": "1", "WXBOT_SELF_CONTAINER": "wxbot-ubuntu-manual"}
        with patch("core.multi_account.subprocess.run") as run:
            response = self._post("/api/accounts/wxbot-ubuntu-manual/stop", env)
        self.assertEqual(response.status_code, 403)
        for call in run.call_args_list:
            self.assertNotIn("stop", call.args[0])
            self.assertNotIn("restart", call.args[0])


if __name__ == "__main__":
    unittest.main()
