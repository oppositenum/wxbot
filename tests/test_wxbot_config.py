import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import wxbot_config  # noqa: E402


class ExportFilterTest(unittest.TestCase):
    def test_key_files_and_backups_never_exported(self):
        j = lambda *p: os.path.join("accounts", "wxid_a", *p)
        for rel in (j("keys.json"), j("keys.json.bak-20261128-195042"), j("keys.json.tmp"),
                    j("decrypted", "message.db"), j("llm_config.json")):
            self.assertFalse(wxbot_config.allowed(rel), rel)
        for rel in (j("bot_rules.json"), j("personas", "x.json"), j("schedules.json")):
            self.assertTrue(wxbot_config.allowed(rel), rel)


if __name__ == "__main__":
    unittest.main()
