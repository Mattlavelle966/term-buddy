import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from term_buddy.config import Config
from term_buddy.events import append_event
from term_buddy.tools import ToolResult
from term_buddy.tui import BuddyUI


class BuddyUiTests(unittest.TestCase):
    def test_project_learning_triggers(self):
        for prompt in [
            "learn project", "Learn my project!", "please index this project", "scan the project",
        ]:
            with self.subTest(prompt=prompt):
                self.assertTrue(BuddyUI.is_project_trigger(prompt))
        self.assertFalse(BuddyUI.is_project_trigger("how does this project work?"))

    def test_restoring_session_does_not_replay_questions(self):
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"
            append_event(events, "question", {"message": "old question", "cwd": "/tmp"})
            ui = BuddyUI(Config(), events, "test", yolo=False, proactive=True)
            ui.restore_session_context()
            self.assertFalse(ui.busy)
            self.assertEqual(list(ui.messages), [])
            self.assertEqual(ui.cwd, "/tmp")
            self.assertGreater(ui.offset, 0)

    def test_model_tool_uses_question_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"
            ui = BuddyUI(Config(), events, "test", yolo=False, proactive=True)
            ui.active_cwd = "/question/directory"
            ui.active_question = "inspect this directory"
            with patch("term_buddy.tui.run_command", return_value=ToolResult("ls", "ok", 0)) as command:
                ui.handle_model_response("<tool>ls</tool>")
                kind, _payload = ui.results.get(timeout=1)
            self.assertEqual(kind, "tool")
            self.assertEqual(command.call_args.kwargs["cwd"], "/question/directory")

    def test_operational_question_omits_loaded_project(self):
        with tempfile.TemporaryDirectory() as directory:
            ui = BuddyUI(Config(), Path(directory) / "events.jsonl", "test", yolo=False, proactive=True)
            ui.project_context = "PROJECT CONTENT" * 100
            context = ui.request_context("ask", "what uncommitted git diff is present?")
            self.assertNotIn("PROJECT CONTENT", context)

    def test_cancel_invalidates_current_request_and_clears_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            ui = BuddyUI(Config(), Path(directory) / "events.jsonl", "test", yolo=False, proactive=True)
            ui.busy = True
            ui.active_request_id = 4
            ui.request_serial = 4
            ui.pending.append(("ask", "later", "/tmp"))
            ui.cancel_current(silent=True)
            self.assertFalse(ui.busy)
            self.assertEqual(ui.active_request_id, 5)
            self.assertEqual(list(ui.pending), [])
