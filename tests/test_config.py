import json
import tempfile
import unittest
from pathlib import Path

from term_buddy.config import Config


class ConfigTests(unittest.TestCase):
    def test_load_known_keys_and_ignore_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"model": "local", "buddy_width": 50, "future_key": True}))
            config = Config.load(path)
            self.assertEqual(config.model, "local")
            self.assertEqual(config.buddy_width, 50)

    def test_save_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Config().save(Path(directory) / "config.json")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            values = json.loads(path.read_text())
            self.assertEqual(set(values), {
                "endpoint", "model", "api_key", "shell", "session_name", "buddy_width",
                "autocomplete", "proactive", "context_watch", "show_activity_panel",
                "web", "searxng_url", "searxng_managed",
            })
