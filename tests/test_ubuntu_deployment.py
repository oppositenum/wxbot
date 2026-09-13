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
        self.assertTrue((ROOT / "deploy.sh").stat().st_mode & 0o111)

    def test_desktop_manager_exposes_only_ubuntu(self):
        from core.desktop_management import PROFILES

        self.assertEqual(list(PROFILES), ["ubuntu"])
        self.assertEqual(PROFILES["ubuntu"]["container"], "wxbot")
        self.assertEqual(PROFILES["ubuntu"]["home"], "/home/wechat")


if __name__ == "__main__":
    unittest.main()
