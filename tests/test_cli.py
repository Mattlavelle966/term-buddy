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

