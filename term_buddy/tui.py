from __future__ import annotations

import curses
import os
import queue
import re
import textwrap
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .config import Config
from .events import append_event, read_events
from .model import ModelClient, ModelError
from .project import ProjectSnapshot, build_project_snapshot
from .tools import ToolDenied, run_command


class BuddyUI:
    def __init__(self, config: Config, events: Path, session: str, *, yolo: bool, proactive: bool):
        self.config = config
        self.events = events
        self.session = session
        self.yolo = yolo
        self.proactive = proactive
        self.client = ModelClient(config)
        self.offset = 0
        self.history: deque[dict] = deque(maxlen=config.context_commands * 3)
        self.messages: deque[tuple[str, str]] = deque(maxlen=300)
        self.results: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.pending: deque[tuple[str, str, str]] = deque(maxlen=20)
        self.busy = False
        self.cwd = os.getcwd()
        self.input_buffer = ""
        self.spinner_frame = 0
        self.active_question = ""
        self.active_cwd = self.cwd
        self.project_context = ""
        self.project_root = ""

    def write(self, label: str, message: str) -> None:
        self.messages.append((label, message.strip()))

    def context(self) -> str:
        chunks: list[str] = []
        for event in list(self.history)[-self.config.context_commands:]:
            if event.get("kind") == "command_finished":
                chunks.append(
                    f"$ {event.get('command', '')}\nexit={event.get('status')} cwd={event.get('cwd')}\n"
                    f"{str(event.get('output', ''))[-self.config.max_output_chars:]}"
                )
            elif event.get("kind") == "tool_result":
                chunks.append(
                    f"Buddy tool: $ {event.get('command', '')}\nexit={event.get('status')}\n"
                    f"{str(event.get('output', ''))[-self.config.max_output_chars:]}"
                )
        terminal = "\n\n".join(chunks)[-self.config.max_output_chars:]
        total_budget = max(8000, self.config.context_window_tokens * 4 - 6000)
        separator = "\n\nRECENT TERMINAL:\n" if self.project_context and terminal else ""
        project_budget = max(0, total_budget - len(terminal) - len(separator))
        project = self.project_context[:project_budget]
        return project + (separator if project else "") + terminal

    def estimated_tokens(self) -> int:
        return max(0, len(self.context()) // 4)

    def request(
        self, kind: str, prompt: str = "", *, continuation: bool = False,
        cwd: str | None = None,
    ) -> None:
        request_cwd = cwd or self.cwd
        if self.busy:
            if kind == "ask":
                self.pending.append((kind, prompt, request_cwd))
            return
        if kind == "ask" and not continuation:
            self.active_question = prompt
        self.active_cwd = request_cwd
        self.busy = True
        context = self.context()

        def worker() -> None:
            try:
                response = self.client.observe(context) if kind == "observe" else self.client.ask(prompt, context)
                self.results.put(("response", response))
            except ModelError as exc:
                self.results.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def is_project_trigger(message: str) -> bool:
        normalized = " ".join(message.lower().strip().split())
        return bool(re.fullmatch(
            r"(?:please )?(?:learn|index|read|study|scan) (?:this |my |the )?project[.!]?",
            normalized,
        ))

    def learn_project(self) -> None:
        if self.busy:
            self.write("Info", "Finish the current model request, then ask me to learn the project again.")
            return
        self.busy = True
        root = self.cwd
        max_chars = max(8000, int(self.config.context_window_tokens * 4 * self.config.project_context_fraction))
        self.write("Info", f"Indexing text files under {root}...")

        def worker() -> None:
            try:
                self.results.put(("project", build_project_snapshot(root, max_chars)))
            except (OSError, subprocess.SubprocessError) as exc:
                self.results.put(("error", f"project indexing failed: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def handle_event(self, event: dict) -> None:
        self.history.append(event)
        self.cwd = event.get("cwd", self.cwd)
        kind = event.get("kind")
        if kind == "question":
            self.write("You", event.get("message", ""))
            if self.is_project_trigger(event.get("message", "")):
                self.learn_project()
            else:
                self.request("ask", event.get("message", ""), cwd=event.get("cwd", self.cwd))
        elif kind == "project_context":
            self.project_context = event.get("content", "")
            self.project_root = event.get("root", "")
        elif kind == "command_finished":
            command = event.get("command", "")
            status = event.get("status", 0)
            if command.strip().startswith("buddy "):
                return
            self.write("Shell", f"[{status}] $ {command}")
            if self.proactive and (status != 0 or command):
                self.request("observe")
        elif kind == "tool_result" and event.get("source") != "model-live":
            self.write("Tool", f"$ {event.get('command')}\n{event.get('output', '')}")

    def handle_model_response(self, message: str) -> None:
        match = re.search(r"<tool>\s*(.*?)\s*</tool>", message, flags=re.DOTALL | re.IGNORECASE)
        if not match:
            if message.strip().upper() != "SILENT":
                self.write("Buddy", message)
                append_event(self.events, "assistant", {"message": message})
            return
        command = match.group(1).strip().strip("`")
        if not self.config.tools:
            self.write("Error", "Buddy tool requests are disabled in configuration.")
            return
        try:
            result = run_command(command, cwd=self.active_cwd, yolo=self.yolo)
        except (ToolDenied, ValueError) as exc:
            self.write("Error", f"Buddy tool request denied: {exc}")
            append_event(self.events, "tool_denied", {"command": command, "message": str(exc)})
            return
        self.write("Tool", f"$ {command}\n{result.output}")
        append_event(self.events, "tool_result", {
            "command": command, "output": result.output, "status": result.returncode,
            "cwd": self.active_cwd, "source": "model-live",
        })
        original = self.active_question or "Review the latest terminal activity."
        followup = (
            f"Original task: {original}\n\nYou requested `{command}`. It exited "
            f"{result.returncode}. Output:\n{result.output}\n\nContinue solving the original task. "
            "Request another tool whenever more evidence is useful."
        )
        self.request("ask", followup, continuation=True, cwd=self.active_cwd)

    def restore_session_context(self) -> None:
        """Load prior context without replaying historical questions or tool actions."""
        events, self.offset = read_events(self.events, 0)
        for event in events:
            self.history.append(event)
            self.cwd = event.get("cwd", self.cwd)
            if event.get("kind") == "project_context":
                self.project_context = event.get("content", "")
                self.project_root = event.get("root", "")

    def handle_input(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return True
        if line in {"/quit", "/exit"}:
            return False
        if line == "/help":
            self.write("Info", "Ask anything, or use /learn, /run COMMAND, /clear, /help, and /quit.")
            return True
        if line == "/clear":
            self.messages.clear()
            return True
        if line == "/learn":
            append_event(self.events, "question", {
                "message": "learn project", "cwd": self.cwd, "source": "buddy-pane",
            })
            return True
        if line.startswith("/run "):
            if not self.config.tools:
                self.write("Error", "Tools are disabled in configuration.")
                return True
            command = line[5:].strip()
            try:
                result = run_command(command, cwd=self.cwd, yolo=self.yolo)
                append_event(self.events, "tool_result", {
                    "command": command, "output": result.output,
                    "status": result.returncode, "cwd": self.cwd,
                })
            except (ToolDenied, ValueError) as exc:
                self.write("Error", str(exc))
            return True
        append_event(self.events, "question", {"message": line, "cwd": self.cwd, "source": "buddy-pane"})
        return True

    @staticmethod
    def _safe_add(window: curses.window, row: int, col: int, value: str, width: int, attr: int = 0) -> None:
        try:
            window.addnstr(row, col, value, max(0, width), attr)
        except curses.error:
            pass

    def render(self, screen: curses.window) -> None:
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 7 or width < 24:
            self._safe_add(screen, 0, 0, "Term Buddy: pane too small", width - 1, curses.A_BOLD)
            screen.refresh()
            return
        mode = "YOLO" if self.yolo else "READ-ONLY"
        endpoint = self.config.endpoint.removeprefix("http://").removeprefix("https://")
        self._safe_add(screen, 0, 0, f" Term Buddy | {self.config.model} @ {endpoint}", width - 1, curses.A_BOLD)
        tokens = self.estimated_tokens()
        spinner = "|/-\\"[self.spinner_frame % 4]
        state = f"thinking {spinner}" if self.busy else "ready"
        auto = "on" if self.config.autocomplete else "off"
        status = (
            f" {state} | context ~{tokens:,}/{self.config.context_window_tokens:,} tokens "
            f"| {mode} | autocomplete {auto}"
        )
        if self.project_root:
            status += " | project loaded"
        self._safe_add(screen, 1, 0, status, width - 1, curses.A_DIM)
        self._safe_add(screen, 2, 0, "─" * (width - 1), width - 1, curses.A_DIM)

        available = height - 5
        rendered: list[tuple[str, int]] = []
        colors = {"Buddy": 1, "You": 2, "Tool": 3, "Error": 4, "Shell": 5, "Info": 5}
        for label, message in self.messages:
            prefix = f"{label}: "
            wrapped = textwrap.wrap(
                message, width=max(10, width - len(prefix) - 1),
                replace_whitespace=False, drop_whitespace=True,
            ) or [""]
            rendered.append((prefix + wrapped[0], colors.get(label, 5)))
            rendered.extend((" " * len(prefix) + part, colors.get(label, 5)) for part in wrapped[1:])
            rendered.append(("", 0))
        for row, (line, color) in enumerate(rendered[-available:], start=3):
            attr = curses.color_pair(color) if curses.has_colors() and color else 0
            self._safe_add(screen, row, 0, line, width - 1, attr)

        prompt_row = height - 2
        self._safe_add(screen, prompt_row - 1, 0, "─" * (width - 1), width - 1, curses.A_DIM)
        prompt = "> " + self.input_buffer
        self._safe_add(screen, prompt_row, 0, prompt, width - 1, curses.A_BOLD)
        try:
            screen.move(prompt_row, min(width - 2, len(prompt)))
        except curses.error:
            pass
        screen.refresh()

    def _run_curses(self, screen: curses.window) -> int:
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        screen.keypad(True)
        screen.nodelay(True)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            curses.init_pair(3, curses.COLOR_MAGENTA, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
            curses.init_pair(5, curses.COLOR_WHITE, -1)
        self.restore_session_context()
        self.write("Info", "Ask here or use `buddy QUESTION` in the shell. /help lists commands.")
        running = True
        while running:
            events, self.offset = read_events(self.events, self.offset)
            for event in events:
                self.handle_event(event)
            try:
                while True:
                    kind, message = self.results.get_nowait()
                    self.busy = False
                    if kind == "response":
                        self.handle_model_response(message)
                    elif kind == "project":
                        snapshot: ProjectSnapshot = message
                        self.project_context = snapshot.content
                        self.project_root = snapshot.root
                        summary = (
                            f"Loaded {snapshot.included}/{snapshot.discovered} text files from "
                            f"{snapshot.root} ({len(snapshot.content):,} characters; "
                            f"{snapshot.skipped_binary} binary and {snapshot.skipped_sensitive} sensitive skipped"
                            + ("; context filled" if snapshot.truncated else "") + ")."
                        )
                        self.write("Info", summary)
                        append_event(self.events, "project_context", {
                            "root": snapshot.root, "content": snapshot.content,
                            "included": snapshot.included, "discovered": snapshot.discovered,
                        })
                        self.request(
                            "ask",
                            "Study the loaded project context. Summarize the architecture, purpose, "
                            "important entry points, and anything risky or surprising. Retain this "
                            "project context for subsequent questions.",
                        )
                    else:
                        self.write("Error", message)
            except queue.Empty:
                pass
            if not self.busy and self.pending:
                pending_kind, pending_prompt, pending_cwd = self.pending.popleft()
                self.request(pending_kind, pending_prompt, cwd=pending_cwd)

            self.spinner_frame += 1
            self.render(screen)
            try:
                key = screen.get_wch()
            except curses.error:
                time.sleep(0.08)
                continue
            if key == curses.KEY_RESIZE:
                continue
            if key in ("\n", "\r"):
                line, self.input_buffer = self.input_buffer, ""
                running = self.handle_input(line)
            elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                self.input_buffer = self.input_buffer[:-1]
            elif key == "\x03":
                self.input_buffer = ""
            elif isinstance(key, str) and key.isprintable():
                self.input_buffer += key
            time.sleep(0.08)
        return 0

    def run(self) -> int:
        return curses.wrapper(self._run_curses)
