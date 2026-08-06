import tempfile
import unittest
from pathlib import Path

from term_buddy.project import build_project_snapshot


class ProjectTests(unittest.TestCase):
    def test_snapshot_reads_text_and_skips_binary_and_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("Project overview")
            (root / "main.py").write_text("print('hello')")
            (root / "image.bin").write_bytes(b"abc\x00def")
            (root / ".env").write_text("SECRET=do-not-send")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "dependency.js").write_text("ignored dependency")
            snapshot = build_project_snapshot(directory, 20000)
            self.assertIn("Project overview", snapshot.content)
            self.assertIn("print('hello')", snapshot.content)
            self.assertNotIn("abc\x00def", snapshot.content)
            self.assertNotIn("do-not-send", snapshot.content)
            self.assertNotIn("ignored dependency", snapshot.content)
            self.assertGreaterEqual(snapshot.included, 2)
            self.assertEqual(snapshot.excluded_directories, 1)

    def test_snapshot_respects_context_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "large.txt").write_text("x" * 30000)
            snapshot = build_project_snapshot(directory, 8000)
            self.assertLessEqual(len(snapshot.content), 8000)
            self.assertTrue(snapshot.truncated)
