import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from term_buddy.completion import complete_buffer


class CompletionTests(unittest.TestCase):
    def test_cd_scans_only_directories_and_extends_common_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scrapcarspickup").mkdir()
            (root / "scrapmetal").mkdir()
            (root / "scrapfile.txt").write_text("x")
            result = complete_buffer("cd scra", directory)
            self.assertEqual(result.suffix, "p")
            self.assertEqual(result.candidates, ["scrapcarspickup/", "scrapmetal/"])

    def test_unique_directory_completion_adds_slash(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "repositories").mkdir()
            result = complete_buffer("cd repo", directory)
            self.assertEqual(result.suffix, "sitories/")

    def test_file_argument_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "README.md").write_text("hello")
            result = complete_buffer("cat REA", directory)
            self.assertEqual(result.suffix, "DME.md")

    def test_first_word_scans_path(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "special-command"
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
            with patch.dict(os.environ, {"PATH": directory}):
                result = complete_buffer("special-c", directory)
            self.assertEqual(result.suffix, "ommand")
