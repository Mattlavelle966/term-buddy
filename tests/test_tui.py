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
            context = ui.request_context("ask", "what was the last commit and its significance?")
            self.assertNotIn("PROJECT CONTENT", context)

    def test_project_context_requires_explicit_project_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            ui = BuddyUI(Config(), Path(directory) / "events.jsonl", "test", yolo=False, proactive=True)
            ui.project_context = "PROJECT CONTENT" * 100
            self.assertNotIn("PROJECT CONTENT", ui.request_context("ask", "change ScrapForm.vue"))
            self.assertIn(
                "PROJECT CONTENT",
                ui.request_context("ask", "change ScrapForm.vue", project_mode="full"),
            )
            self.assertEqual(
                ui.parse_question_mode("/proj make ScrapForm green"),
                ("project", "make ScrapForm green"),
            )
            self.assertEqual(
                ui.parse_question_mode("/proj-full inspect everything"),
                ("full", "inspect everything"),
            )
            self.assertTrue(ui.is_operational_question("tell me about the last 2 commits"))
            self.assertFalse(ui.is_operational_question("make ScrapForm green"))

    def test_explicit_markdown_tool_plan_is_recovered(self):
        message = "The next step is clear.\n\n**Next tool request:**\n```\ngit show HEAD\n```"
        self.assertEqual(BuddyUI.extract_tool_request(message), "git show HEAD")
        self.assertEqual(BuddyUI.extract_tool_request("ordinary ```bash\nls\n``` advice"), "")

    def test_project_context_keeps_token_headroom(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(context_window_tokens=10000, chars_per_token_estimate=3.0, project_context_fraction=0.8)
            ui = BuddyUI(config, Path(directory) / "events.jsonl", "test", yolo=False, proactive=True)
            ui.project_context = "x" * 100000
            self.assertLessEqual(ui.estimated_tokens(), 8000)

    def test_rewrite_followup_uses_chat_without_project(self):
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"
            ui = BuddyUI(Config(), events, "test", yolo=False, proactive=True)
            ui.project_context = "PROJECT CONTENT" * 1000
            ui.history.append({"kind": "question", "message": "explain recent commits"})
            ui.history.append({"kind": "assistant", "message": "a long explanation"})
            context = ui.request_context("ask", "make your previous answer shorter")
            self.assertIn("a long explanation", context)
            self.assertNotIn("PROJECT CONTENT", context)
            self.assertTrue(ui.is_rewrite_followup("summarize that answer"))

    def test_cancel_invalidates_current_request_and_clears_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            ui = BuddyUI(Config(), Path(directory) / "events.jsonl", "test", yolo=False, proactive=True)
            ui.busy = True
            ui.active_request_id = 4
            ui.request_serial = 4
            ui.pending.append(("ask", "later", "/tmp", "none"))
            ui.cancel_current(silent=True)
            self.assertFalse(ui.busy)
            self.assertEqual(ui.active_request_id, 5)
            self.assertEqual(list(ui.pending), [])

    def test_new_question_interrupts_and_carries_partial_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(interrupt_on_new_question=True)
            ui = BuddyUI(config, Path(directory) / "events.jsonl", "test", yolo=False, proactive=True)
            ui.busy = True
            ui.active_kind = "ask"
            ui.active_question = "give me a long report"
            ui.stream_text = "partial report text"
            ui.request_serial = 1
            ui.active_request_id = 1
            with patch("term_buddy.model.ModelClient.cancel"), patch("threading.Thread.start"):
                ui.request("ask", "make it shorter")
            self.assertTrue(ui.busy)
            self.assertIn("partial report text", ui.active_question)
            self.assertIn("make it shorter", ui.active_question)
            self.assertEqual(list(ui.pending), [])

    def test_question_interrupting_observation_does_not_inherit_old_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            ui = BuddyUI(Config(), Path(directory) / "events.jsonl", "test", yolo=False, proactive=True)
            ui.busy = True
            ui.active_kind = "observe"
            ui.active_question = "give me a shorter run down"
            ui.stream_text = "old observation"
            with patch("term_buddy.model.ModelClient.cancel"), patch("threading.Thread.start"):
                ui.request("ask", "what command finds a component directory?")
            self.assertEqual(ui.active_question, "what command finds a component directory?")
