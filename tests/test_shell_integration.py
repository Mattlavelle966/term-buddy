import os
import pty
import select
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from term_buddy.events import read_events


class ShellIntegrationTests(unittest.TestCase):
    def test_shell_hook_preserves_prompt_command_arrays(self):
        root = Path(__file__).resolve().parent.parent
        script = root / "term_buddy/shell/term-buddy.bash"
        command = (
            "PROMPT_COMMAND=('printf first' 'printf second'); "
            f"source {script}; declare -p PROMPT_COMMAND"
        )
        environment = os.environ.copy()
        environment.update({
            "TERM_BUDDY_EVENTS": "/tmp/term-buddy-array-test/events.jsonl",
            "TERM_BUDDY_SESSION": "array-test",
            "TERM_BUDDY_LAUNCHER": "/bin/true",
        })
        completed = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", command],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("printf first", completed.stdout)
        self.assertIn("printf second", completed.stdout)
        self.assertIn("__term_buddy_emit_prompt", completed.stdout)
        self.assertIn("__term_buddy_prompt_marker", completed.stdout)

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

    def test_shift_tab_completes_a_live_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(__file__).resolve().parent.parent
            runtime = Path(directory) / "runtime"
            project = Path(directory) / "project"
            runtime.mkdir()
            project.mkdir()
            (project / "repositories").mkdir()
            events = runtime / "events.jsonl"
            (runtime / "transcript.log").write_text("shell transcript\n")
            (runtime / "autocomplete.enabled").write_text("on\n")
            master, slave = pty.openpty()
            environment = os.environ.copy()
            environment.update({
                "TERM_BUDDY_EVENTS": str(events),
                "TERM_BUDDY_SESSION": "completion-test",
                "TERM_BUDDY_LAUNCHER": str(root / "bin" / "term-buddy"),
                "TERM_BUDDY_AUTOCOMPLETE": "1",
            })
            process = subprocess.Popen(
                ["bash", "--noprofile", "--norc", "-i"],
                stdin=slave, stdout=slave, stderr=slave, cwd=project, env=environment,
            )
            os.close(slave)
            os.write(master, f"source {root / 'term_buddy/shell/term-buddy.bash'}\n".encode())
            os.write(master, b"cd repo\x1b[Z\npwd\nexit\n")
            process.wait(timeout=10)
            os.close(master)
            captured, _ = read_events(events)
            completed = [event for event in captured if event.get("kind") == "command_finished"]
            self.assertTrue(any(event.get("command") == "cd repositories/" for event in completed))
            self.assertTrue(any(event.get("cwd") == str(project / "repositories") for event in completed))

    def test_input_adapter_draws_virtual_ghost_and_shift_tab_accepts_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(__file__).resolve().parent.parent
            runtime = Path(directory) / "runtime"
            project = Path(directory) / "project"
            runtime.mkdir()
            project.mkdir()
            (project / "repositories").mkdir()
            events = runtime / "events.jsonl"
            (runtime / "transcript.log").write_text("shell transcript\n")
            (runtime / "autocomplete.enabled").write_text("on\n")
            bashrc = Path(directory) / "bashrc"
            bashrc.write_text(f"source {root / 'term_buddy/shell/term-buddy.bash'}\n")
            master, slave = pty.openpty()
            environment = os.environ.copy()
            environment.update({
                "TERM_BUDDY_EVENTS": str(events),
                "TERM_BUDDY_SESSION": "adapter-test",
                "TERM_BUDDY_LAUNCHER": str(root / "bin" / "term-buddy"),
            })
            process = subprocess.Popen(
                [
                    str(root / "bin" / "term-buddy"), "_shell", "--events", str(events), "--",
                    "bash", "--rcfile", str(bashrc), "-i",
                ],
                stdin=slave, stdout=slave, stderr=slave, cwd=project, env=environment,
            )
            os.close(slave)
            output = b""
            time.sleep(0.3)
            while select.select([master], [], [], 0.02)[0]:
                output += os.read(master, 65536)
            os.write(master, b"cd repo")
            time.sleep(0.35)
            while select.select([master], [], [], 0.02)[0]:
                output += os.read(master, 65536)
            self.assertIn(b"\x1b[90msitories/\x1b[0m", output)
            # Enter without Shift-Tab must execute only the real Bash buffer.
            os.write(master, b"\n")
            time.sleep(0.25)
            os.write(master, b"cd repo")
            time.sleep(0.25)
            os.write(master, b"\x1b[Z\npwd\nexit\n")
            process.wait(timeout=10)
            os.close(master)
            captured, _ = read_events(events)
            completed = [event for event in captured if event.get("kind") == "command_finished"]
            self.assertTrue(any(
                event.get("command") == "cd repo" and event.get("status") != 0
                for event in completed
            ))
            self.assertTrue(any(event.get("command") == "cd repositories/" for event in completed))
            self.assertTrue(any(event.get("cwd") == str(project / "repositories") for event in completed))
