"""Deployment must remain a single-entry Ubuntu container."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class UbuntuDeployment(unittest.TestCase):
    def test_production_image_is_ubuntu_only(self):
        dockerfile = (ROOT / "docker" / "Dockerfile").read_text()
        self.assertIn("FROM ubuntu:24.04", dockerfile)
        self.assertIn('com.oppositenum.wxbot.runtime="ubuntu-24.04"', dockerfile)
        self.assertNotIn("from debian", dockerfile.lower())

    def test_compose_uses_ubuntu_home_and_canonical_image(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertIn("dockerfile: docker/Dockerfile", compose)
        self.assertIn("wxbot-wechat:ubuntu-24.04", compose)
        self.assertIn("wxdata:/home/wechat", compose)
        self.assertNotIn("wxdata:/root", compose)

    def test_one_command_entrypoint_validates_image(self):
        deploy = (ROOT / "deploy.sh").read_text()
        self.assertIn("docker compose up -d", deploy)
        self.assertIn("is_ubuntu_image", deploy)
        self.assertIn("ubuntu-24.04", deploy)
        self.assertIn("IMAGE_OVERRIDE", deploy)
        self.assertIn("WXBOT_UI_USER", deploy)
        self.assertIn("WXBOT_UI_PASSWORD", deploy)
        self.assertTrue((ROOT / "deploy.sh").stat().st_mode & 0o111)

    def test_ui_login_is_env_only_not_hardcoded(self):
        server = (ROOT / "server.py").read_text()
        example = (ROOT / ".env.example").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertNotIn('or "default-user"', server)
        self.assertNotIn('or "default-password"', server)
        self.assertIn("WXBOT_UI_USER", example)
        self.assertIn("WXBOT_UI_PASSWORD", example)
        self.assertIn("WXBOT_SECRET", example)
        self.assertIn("WXBOT_UI_USER", compose)
        self.assertIn("WXBOT_UI_PASSWORD", compose)

    def test_legacy_compose_reuses_root_env_without_overrides(self):
        compose = (ROOT / "docker" / "ubuntu-manual" / "compose.yaml").read_text()
        self.assertIn("env_file:\n      - ../../.env", compose)
        self.assertIn("${WXBOT_IMAGE:-wxbot-ubuntu-manual:24.04}", compose)
        self.assertNotIn("WXBOT_UI_PASSWORD:", compose)
        self.assertNotIn("WXBOT_SECRET:", compose)
        self.assertNotIn("VNC_PASSWORD:", compose)
        self.assertIn("../../server.py:/app/server.py:ro", compose)
        self.assertIn("../../core:/app/core:ro", compose)
        self.assertIn("../../static:/app/static:ro", compose)

    def test_desktop_manager_exposes_only_ubuntu(self):
        from core.desktop_management import DEFAULT_PROFILES

        self.assertEqual(list(DEFAULT_PROFILES), ["primary"])
        self.assertEqual(DEFAULT_PROFILES["primary"]["container"], "wxbot-ubuntu-manual")
        self.assertEqual(DEFAULT_PROFILES["primary"]["home"], "/home/wechat")
        self.assertIn("Ubuntu 24.04", DEFAULT_PROFILES["primary"]["system"])


if __name__ == "__main__":
    unittest.main()
