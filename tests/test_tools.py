import tempfile
import unittest
from pathlib import Path

from term_buddy.tools import ToolDenied, run_command


class ToolTests(unittest.TestCase):
    def test_read_only_command_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_command("pwd", cwd=directory, yolo=False)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.output.strip(), directory)

    def test_mutation_is_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in [
                "rm file", "git checkout main", "find . -delete", "awk BEGIN{system(\"touch /tmp/x\")}",
                "sed -i s/a/b/ file", "cat x > y", "rg --pre touch pattern", "git status --output=x",
                "man --pager=touch ls",
            ]:
                with self.subTest(command=command), self.assertRaises(ToolDenied):
                    run_command(command, cwd=directory, yolo=False)

    def test_yolo_can_write(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "created"
            result = run_command(f"touch {target}", cwd=directory, yolo=True)
            self.assertEqual(result.returncode, 0)
            self.assertTrue(target.exists())
