from __future__ import annotations

import fcntl
import os
import pty
import re
import selectors
import signal
import sys
import termios
import time
import tty
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .completion import complete_buffer


PROMPT_MARKER = re.compile(rb"\x1b\]777;term-buddy-prompt\x07")
SHIFT_TAB = b"\x1b[Z"
DIM = b"\x1b[90m"
RESET = b"\x1b[0m"
CLEAR_RIGHT = b"\x1b[K"
GHOST_START = b"\x1b]777;term-buddy-ghost-start\x07"
GHOST_END = b"\x1b]777;term-buddy-ghost-end\x07"
ANSI_ESCAPE = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def display_width(value: str) -> int:
    """Return the terminal cell width needed to paint and erase a ghost."""
    width = 0
    for character in value:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width


def visible_tail_width(value: bytes) -> int:
    """Measure the visible final line of terminal output such as a styled PS1."""
    plain = ANSI_ESCAPE.sub(b"", value).decode(errors="replace")
    tail = re.split(r"[\r\n]", plain)[-1].expandtabs(8)
    return display_width(tail)


def truncate_cells(value: str, limit: int) -> str:
    """Return the longest prefix that occupies no more than ``limit`` cells."""
    if limit <= 0:
        return ""
    result: list[str] = []
    used = 0
    for character in value:
        character_width = display_width(character)
        if used + character_width > limit:
            break
        result.append(character)
        used += character_width
    return "".join(result)


@dataclass(slots=True)
class LineState:
    text: str = ""
    cursor: int = 0
    prompt_active: bool = False
    cwd: str = ""
    ghost: str = ""
    ghost_width: int = 0
    prompt_width: int = 0
    changed_at: float = 0.0
    valid: bool = True

    def reset_prompt(self, cwd: str) -> None:
        self.text = ""
        self.cursor = 0
        self.prompt_active = True
        self.cwd = cwd
        self.ghost = ""
        self.ghost_width = 0
        self.prompt_width = 0
        self.changed_at = time.monotonic()
        self.valid = True

    def insert(self, value: str) -> None:
        self.text = self.text[:self.cursor] + value + self.text[self.cursor:]
        self.cursor += len(value)
        self.changed_at = time.monotonic()

    def backspace(self) -> None:
        if self.cursor:
            self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
            self.cursor -= 1
        self.changed_at = time.monotonic()

    def preview(self) -> str:
        if not self.valid or not self.prompt_active or self.cursor != len(self.text) or not self.text.strip():
            return ""
        result = complete_buffer(self.text, self.cwd, self.cursor)
        if result.suffix:
            return result.suffix
        if result.candidates:
            match = re.search(r"(?:^|\s)((?:\\.|[^\s])*)$", self.text[:self.cursor])
            token = match.group(1) if match else ""
            first = result.candidates[0]
            if first.startswith(token):
                return first[len(token):]
        return ""


class InputAdapter:
    def __init__(self, command: list[str], events: Path):
        self.command = command
        self.events = events
        self.autocomplete_flag = events.parent / "autocomplete.enabled"
        self.state = LineState(cwd=os.getcwd())
        self.master_fd = -1
        self.original_terminal: list | None = None
        self.scan_tail = b""
        self.prompt_output = b""
        self.collecting_prompt = False
        self.running = True
        self.child_pid = -1

    @staticmethod
    def _write(fd: int, data: bytes) -> None:
        while data:
            try:
                written = os.write(fd, data)
                data = data[written:]
            except InterruptedError:
                continue
            except OSError:
                return

    def _clear_ghost(self) -> None:
        if self.state.ghost:
            self._write(sys.stdout.fileno(), CLEAR_RIGHT)
            self.state.ghost = ""
            self.state.ghost_width = 0

    def _accept_ghost(self) -> bool:
        if not self.state.ghost:
            return False
        suffix = self.state.ghost
        self._clear_ghost()
        self._write(self.master_fd, suffix.encode())
        self.state.insert(suffix)
        return True

    def _forward_key(self, data: bytes) -> None:
        self.collecting_prompt = False
        if data == SHIFT_TAB and self.autocomplete_flag.exists() and self.state.ghost:
            self._accept_ghost()
            return
        self._clear_ghost()
        self._write(self.master_fd, data)
        if not self.state.prompt_active:
            return
        if data == SHIFT_TAB:
            self.state.valid = False  # Bash's on-demand/AI completer now owns the buffer.
        elif data in {b"\r", b"\n"}:
            self.state.prompt_active = False
            self.state.text = ""
            self.state.cursor = 0
        elif data in {b"\x7f", b"\b"}:
            self.state.backspace()
        elif data == b"\x03":
            self.state.text = ""
            self.state.cursor = 0
            self.state.valid = True
        elif data == b"\x01":
            self.state.cursor = 0
        elif data == b"\x05":
            self.state.cursor = len(self.state.text)
        elif data == b"\x15":
            self.state.text = self.state.text[self.state.cursor:]
            self.state.cursor = 0
        elif data == b"\x0b":
            self.state.text = self.state.text[:self.state.cursor]
        elif data == b"\x1b[D":
            self.state.cursor = max(0, self.state.cursor - 1)
        elif data == b"\x1b[C":
            self.state.cursor = min(len(self.state.text), self.state.cursor + 1)
        elif len(data) == 1 and 32 <= data[0] < 127:
            self.state.insert(data.decode())
        elif data.startswith(b"\x1b") or any(byte < 32 for byte in data):
            self.state.valid = False
        else:
            self.state.valid = False

    def _handle_input(self, data: bytes) -> None:
        sequences = {SHIFT_TAB, b"\x1b[D", b"\x1b[C", b"\x1b[H", b"\x1b[F"}
        if data in sequences or len(data) == 1:
            self._forward_key(data)
            return
        index = 0
        while index < len(data):
            sequence = next((item for item in sequences if data.startswith(item, index)), None)
            if sequence:
                self._forward_key(sequence)
                index += len(sequence)
            else:
                self._forward_key(data[index:index + 1])
                index += 1

    def _handle_child_output(self, data: bytes) -> None:
        if self.state.ghost:
            self._clear_ghost()
        self._write(sys.stdout.fileno(), data)
        scan = self.scan_tail + data
        old_length = len(self.scan_tail)
        if len(scan) > 8192:
            removed = len(scan) - 8192
            scan = scan[-8192:]
            old_length = max(0, old_length - removed)
        markers = [match for match in PROMPT_MARKER.finditer(scan) if match.end() > old_length]
        if markers:
            try:
                cwd = os.readlink(f"/proc/{self.child_pid}/cwd")
            except OSError:
                cwd = self.state.cwd or os.getcwd()
            self.state.reset_prompt(cwd)
            self.prompt_output = scan[markers[-1].end():]
            self.collecting_prompt = True
            self.state.prompt_width = visible_tail_width(self.prompt_output)
        elif self.collecting_prompt:
            self.prompt_output = (self.prompt_output + data)[-4096:]
            self.state.prompt_width = visible_tail_width(self.prompt_output)
        self.scan_tail = scan[-512:]

    def _terminal_columns(self) -> int:
        try:
            return os.get_terminal_size(sys.stdout.fileno()).columns
        except OSError:
            return 0

    def _visible_preview(self, suffix: str) -> str:
        """Clip a ghost to its current row so cursor-left remains reliable."""
        columns = self._terminal_columns()
        if columns <= 0:
            return suffix
        real_width = self.state.prompt_width + display_width(self.state.text[:self.state.cursor])
        cursor_column = real_width % columns
        # Never paint the last cell: many terminals enter a pending autowrap
        # state there even before another character is printed.
        available = columns - cursor_column - 1
        return truncate_cells(suffix, available)

    def _render_preview(self) -> None:
        if (
            self.state.ghost or not self.autocomplete_flag.exists()
            or time.monotonic() - self.state.changed_at < 0.14
        ):
            return
        suffix = self.state.preview()
        if not suffix or any(ord(character) < 32 or ord(character) == 127 for character in suffix):
            return
        visible = self._visible_preview(suffix)
        if not visible:
            return
        self.state.ghost = suffix
        self.state.ghost_width = display_width(visible)
        encoded = visible.encode(errors="replace")
        self._write(sys.stdout.fileno(), GHOST_START + DIM + encoded + RESET + GHOST_END)
        self._write(sys.stdout.fileno(), f"\x1b[{self.state.ghost_width}D".encode())

    def _resize_child(self) -> None:
        if self.master_fd < 0:
            return
        try:
            size = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, size)
        except OSError:
            pass

    def _stop_child(self) -> None:
        self.running = False
        if self.child_pid > 0:
            try:
                os.kill(self.child_pid, signal.SIGHUP)
            except ProcessLookupError:
                pass

    def run(self) -> int:
        if not self.command:
            raise ValueError("input adapter requires a shell command")
        self.child_pid, self.master_fd = pty.fork()
        if self.child_pid == 0:
            os.execvpe(self.command[0], self.command, os.environ.copy())
        stdin_fd = sys.stdin.fileno()
        self.original_terminal = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)
        self._resize_child()
        signal.signal(signal.SIGWINCH, lambda _signum, _frame: self._resize_child())
        signal.signal(signal.SIGHUP, lambda _signum, _frame: self._stop_child())
        signal.signal(signal.SIGTERM, lambda _signum, _frame: self._stop_child())
        selector = selectors.DefaultSelector()
        selector.register(stdin_fd, selectors.EVENT_READ, "input")
        selector.register(self.master_fd, selectors.EVENT_READ, "child")
        status = 0
        try:
            while self.running:
                for key, _mask in selector.select(timeout=0.04):
                    try:
                        data = os.read(key.fd, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        if key.data == "input":
                            self._stop_child()
                        self.running = False
                        break
                    if key.data == "input":
                        self._handle_input(data)
                    else:
                        self._handle_child_output(data)
                self._render_preview()
        finally:
            self._clear_ghost()
            if self.original_terminal is not None:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, self.original_terminal)
            selector.close()
            try:
                _child, wait_status = os.waitpid(self.child_pid, 0)
                status = os.waitstatus_to_exitcode(wait_status)
            except ChildProcessError:
                pass
        return status


def run_input_adapter(command: list[str], events: Path) -> int:
    return InputAdapter(command, events).run()
