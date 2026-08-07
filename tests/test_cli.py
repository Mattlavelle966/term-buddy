import tempfile
import unittest
from pathlib import Path

from term_buddy.cli import transcript_tail


class CliTests(unittest.TestCase):
    def test_transcript_tail_strips_terminal_escapes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "transcript.log").write_bytes(b"before\n\x1b[31mfailed\x1b[0m\n")
            output = transcript_tail(root / "events.jsonl", 1000)
            self.assertEqual(output, "before\nfailed\n")

    def test_transcript_tail_removes_visual_ghost_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "transcript.log").write_bytes(
                b"cd repo\x1b]777;term-buddy-ghost-start\x07\x1b[90msitories/\x1b[0m"
                b"\x1b]777;term-buddy-ghost-end\x07\x1b[9D\n"
            )
            output = transcript_tail(root / "events.jsonl", 1000)
            self.assertNotIn("sitories", output)
            self.assertIn("cd repo", output)
