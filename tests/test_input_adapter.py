import tempfile
import unittest
from pathlib import Path

from term_buddy.input_adapter import (
    InputAdapter,
    LineState,
    PROMPT_MARKER,
    SHIFT_TAB,
    display_width,
    visible_tail_width,
)


class RecordingAdapter(InputAdapter):
    def __init__(self, events: Path):
        super().__init__(["bash"], events)
        self.writes: list[tuple[int, bytes]] = []
        self.master_fd = 99
        self.columns = 80

    def _write(self, fd: int, data: bytes) -> None:
        self.writes.append((fd, data))

    def _terminal_columns(self) -> int:
        return self.columns


class InputAdapterTests(unittest.TestCase):
    def test_preview_is_virtual_and_does_not_change_line(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "repositories").mkdir()
            state = LineState()
            state.reset_prompt(directory)
            state.insert("cd repo")
            self.assertEqual(state.preview(), "sitories/")
            self.assertEqual(state.text, "cd repo")

    def test_shift_tab_accepts_visible_ghost_without_enter(self):
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"
            (Path(directory) / "autocomplete.enabled").write_text("on\n")
            adapter = RecordingAdapter(events)
            adapter.state.reset_prompt(directory)
            adapter.state.insert("cd repo")
            adapter.state.ghost = "sitories/"
            adapter._forward_key(SHIFT_TAB)
            self.assertEqual(adapter.state.text, "cd repositories/")
            self.assertTrue(any(fd == 99 and data == b"sitories/" for fd, data in adapter.writes))
            self.assertTrue(adapter.state.prompt_active)

    def test_prompt_marker_is_detectable(self):
        marker = b"\x1b]777;term-buddy-prompt\x07"
        self.assertIsNotNone(PROMPT_MARKER.search(marker))

    def test_wrapping_ghost_is_clipped_before_terminal_edge(self):
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"
            Path(directory, "autocomplete.enabled").write_text("on\n")
            Path(directory, "repositories").mkdir()
            adapter = RecordingAdapter(events)
            adapter.state.reset_prompt(directory)
            adapter.state.insert("cd repo")
            adapter.state.changed_at = 0
            adapter.columns = 14

            adapter._render_preview()
            self.assertIn(b"\x1b[90msitori\x1b[0m", adapter.writes[-2][1])
            self.assertEqual(adapter.writes[-1][1], b"\x1b[6D")
            # Acceptance retains the entire completion, not just its preview.
            self.assertEqual(adapter.state.ghost, "sitories/")

    def test_display_width_handles_wide_and_combining_characters(self):
        self.assertEqual(display_width("abc界e\u0301"), 6)

    def test_visible_prompt_width_ignores_terminal_styling(self):
        self.assertEqual(visible_tail_width(b"old\n\x1b[31mproject\x1b[0m \xe2\x9d\xaf "), 10)
