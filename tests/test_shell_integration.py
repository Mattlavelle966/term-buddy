import os
import pty
import subprocess
import tempfile
import unittest
from pathlib import Path

from term_buddy.events import read_events


class ShellIntegrationTests(unittest.TestCase):
    def test_duplicate_commands_are_emitted_with_histcontrol_ignoredups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(__file__).resolve().parent.parent
            events = Path(directory) / "events.jsonl"
            (Path(directory) / "transcript.log").write_text("shell transcript\n")
            master, slave = pty.openpty()
            environment = os.environ.copy()
            environment.update({
                "TERM_BUDDY_EVENTS": str(events),
                "TERM_BUDDY_SESSION": "shell-test",
                "TERM_BUDDY_LAUNCHER": str(root / "bin" / "term-buddy"),
                "TERM_BUDDY_AUTOCOMPLETE": "0",
            })
            process = subprocess.Popen(
                ["bash", "--noprofile", "--norc", "-i"],
                stdin=slave, stdout=slave, stderr=slave, cwd=root, env=environment,
            )
            os.close(slave)
            commands = (
                f"source {root / 'term_buddy/shell/term-buddy.bash'}\n"
                "HISTCONTROL=ignoredups\n"
                "cd /tmp\n"
                "cd /tmp\n"
                "exit\n"
            )
            os.write(master, commands.encode())
            process.wait(timeout=10)
            os.close(master)
            captured, _ = read_events(events)
            repeated = [
                event for event in captured
                if event.get("kind") == "command_finished" and event.get("command") == "cd /tmp"
            ]
            self.assertEqual(len(repeated), 2)
