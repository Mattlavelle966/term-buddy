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
from .project import build_project_snapshot
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
        self.pending: deque[tuple[str, str, str, bool]] = deque(maxlen=20)
        self.busy = False
        self.active_kind = ""
        self.request_serial = 0
        self.active_request_id = 0
        self.cwd = os.getcwd()
        self.input_buffer = ""
        self.spinner_frame = 0
        self.active_question = ""
        self.active_project_mode = False
        self.active_cwd = self.cwd
        self.project_context = ""
        self.project_root = ""
        self.stream_text = ""
        self.request_started = 0.0
        self.request_context_tokens = 0
        self.first_delta_at = 0.0
        self.reasoning_chars = 0
        self.reasoning_started = 0.0
        self.activity_expanded = config.show_activity_panel
        self.activity_log: deque[str] = deque(maxlen=8)

    def write(self, label: str, message: str) -> None:
        self.messages.append((label, message.strip()))

    def terminal_context(self) -> str:
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
        return "\n\n".join(chunks)[-self.config.max_output_chars:]

    def conversation_context(self) -> str:
        messages: list[str] = []
        for event in list(self.history)[-16:]:
            if event.get("kind") == "question":
                messages.append(f"User: {event.get('message', '')}")
            elif event.get("kind") == "assistant":
                messages.append(f"Buddy: {event.get('message', '')}")
        return "\n\n".join(messages)[-(self.config.max_output_chars * 2):]

    def context(self) -> str:
        terminal = self.terminal_context()
        conversation = self.conversation_context()
        recent = "\n\n".join(part for part in (terminal, conversation) if part)
        total_budget = max(
            8000,
            int(
                self.config.context_window_tokens
                * self.config.chars_per_token_estimate
                * self.config.project_context_fraction
            ),
        )
        separator = "\n\nRECENT TERMINAL AND CHAT:\n" if self.project_context and recent else ""
        project_budget = max(0, total_budget - len(recent) - len(separator))
        project = self.project_context[:project_budget]
        return project + (separator if project else "") + recent

    def estimated_tokens(self) -> int:
        return max(0, int(len(self.context()) / self.config.chars_per_token_estimate))

    def request_context(self, kind: str, prompt: str, *, project_mode: bool = False) -> str:
        if kind == "observe":
            return self.terminal_context()
        conversation = self.conversation_context()
        lightweight = "\n\n".join(part for part in (self.terminal_context(), conversation) if part)
        return self.context() if project_mode else lightweight

    @staticmethod
    def is_rewrite_followup(prompt: str) -> bool:
        return bool(re.search(
            r"\b(shorter|summari[sz]e|condense|rewrite|rephrase|tl;?dr|previous answer|"
            r"last answer|that answer|your answer)\b",
            prompt,
            flags=re.IGNORECASE,
        ))

    def request(
        self, kind: str, prompt: str = "", *, continuation: bool = False,
        cwd: str | None = None, project_mode: bool | None = None,
    ) -> None:
        request_cwd = cwd or self.cwd
        requested_project_mode = self.active_project_mode if continuation else bool(project_mode)
        if self.busy:
            if kind == "ask" and not continuation and self.config.interrupt_on_new_question:
                previous = self.active_question
                partial = self.stream_text[-8000:]
                self.cancel_current(silent=True)
                if previous or partial:
                    prompt = (
                        f"Previous request: {previous}\n\nPartial answer before interruption:\n"
                        f"{partial or '[no answer tokens yet]'}\n\nNew user instruction: {prompt}"
                    )
                self.write("Info", "Interrupted the previous Buddy task for the new question.")
            elif self.active_kind == "observe" and kind == "ask":
                self.cancel_current(silent=True)
            else:
                if kind == "ask":
                    self.pending.append((kind, prompt, request_cwd, requested_project_mode))
                return
        if kind == "ask" and not continuation:
            if self.is_rewrite_followup(prompt):
                prompt = (
                    "Rewrite or summarize the previous Buddy answer as requested. Use the chat "
                    "context, do not inspect the project again, and do not request tools.\n\n" + prompt
                )
            self.active_question = prompt
            self.active_project_mode = requested_project_mode
        self.active_cwd = request_cwd
        self.request_serial += 1
        request_id = self.request_serial
        self.active_request_id = request_id
        self.active_kind = kind
        self.busy = True
        context = self.request_context(kind, prompt, project_mode=requested_project_mode)
        self.stream_text = ""
        self.request_started = time.monotonic()
        self.first_delta_at = 0.0
        self.reasoning_chars = 0
        self.reasoning_started = 0.0
        self.request_context_tokens = int(len(context) / self.config.chars_per_token_estimate)
        scope = "project + terminal" if requested_project_mode else "lightweight terminal + chat"
        self.activity_log.append(f"Request started: {kind}; {scope}; ~{self.request_context_tokens:,} tokens")

        def worker() -> None:
            try:
                chunks: list[str] = []
                def activity(event_kind: str, value: str) -> None:
                    self.results.put(("activity", (request_id, event_kind, len(value))))

                stream = (
                    self.client.observe_stream(context, activity_callback=activity)
                    if kind == "observe"
                    else self.client.ask_stream(prompt, context, activity_callback=activity)
                )
                for delta in stream:
                    chunks.append(delta)
                    self.results.put(("delta", (request_id, delta)))
                response = "".join(chunks).strip()
                self.results.put(("response", (request_id, response)))
            except ModelError as exc:
                self.results.put(("error", (request_id, str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    def cancel_current(self, *, silent: bool = False) -> None:
        had_work = self.busy or bool(self.pending)
        previous_kind = self.active_kind
        self.request_serial += 1
        self.active_request_id = self.request_serial
        self.active_kind = ""
        self.busy = False
        self.client.cancel()
        self.stream_text = ""
        self.reasoning_chars = 0
        self.reasoning_started = 0.0
        self.pending.clear()
        if had_work:
            self.activity_log.append(f"Cancelled {previous_kind or 'queued'} task")
        if had_work and not silent:
            self.write("Info", "Stopped the current Buddy request and cleared queued requests.")

    @staticmethod
    def is_project_trigger(message: str) -> bool:
        normalized = " ".join(message.lower().strip().split())
        return bool(re.fullmatch(
            r"(?:please )?(?:learn|index|read|study|scan) (?:this |my |the )?project[.!]?",
            normalized,
        ))

    def learn_project(self) -> None:
        if self.busy:
            self.cancel_current(silent=True)
        self.busy = True
        self.request_serial += 1
        request_id = self.request_serial
        self.active_request_id = request_id
        self.active_kind = "project"
        root = self.cwd
        max_chars = max(
            8000,
            int(
                self.config.context_window_tokens
                * self.config.chars_per_token_estimate
                * self.config.project_context_fraction
            ),
        )
        self.write("Info", f"Indexing text files under {root}...")

        def worker() -> None:
            try:
                self.results.put(("project", (request_id, build_project_snapshot(root, max_chars))))
            except (OSError, subprocess.SubprocessError) as exc:
                self.results.put(("error", (request_id, f"project indexing failed: {exc}")))

        threading.Thread(target=worker, daemon=True).start()

    def handle_event(self, event: dict) -> None:
        self.history.append(event)
        self.cwd = event.get("cwd", self.cwd)
        kind = event.get("kind")
        if kind == "question":
            if event.get("message", "").strip().lower() in {"stop", "cancel", "stop thinking"}:
                self.cancel_current()
                return
            raw_message = event.get("message", "")
            self.write("You", raw_message)
            if self.is_project_trigger(raw_message):
                self.learn_project()
            else:
                project_mode, message = self.parse_question_mode(raw_message)
                if project_mode and not self.project_context:
                    self.write("Error", "No project is loaded. Run `buddy learn project` first.")
                    return
                if project_mode and not message:
                    self.write("Error", "Usage: buddy /proj QUESTION")
                    return
                self.request(
                    "ask", message, cwd=event.get("cwd", self.cwd),
                    project_mode=project_mode,
                )
        elif kind == "project_context":
            self.project_context = event.get("content", "")
            self.project_root = event.get("root", "")
        elif kind == "command_finished":
            command = event.get("command", "")
            status = event.get("status", 0)
            if command.strip().startswith("buddy "):
                return
            if self.busy and self.active_kind == "observe":
                self.cancel_current(silent=True)
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
        self.activity_log.append(f"Tool requested: {command}")
        if not self.config.tools:
            self.write("Error", "Buddy tool requests are disabled in configuration.")
            return
        request_id = self.active_request_id
        cwd = self.active_cwd
        original = self.active_question or "Review the latest terminal activity."
        self.busy = True
        self.active_kind = "tool"
        self.request_started = time.monotonic()

        def worker() -> None:
            try:
                result = run_command(command, cwd=cwd, yolo=self.yolo)
                self.results.put(("tool", (request_id, command, cwd, original, result)))
            except (ToolDenied, ValueError) as exc:
                self.results.put(("tool_error", (request_id, command, str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    def restore_session_context(self) -> None:
        """Load prior context without replaying historical questions or tool actions."""
        events, self.offset = read_events(self.events, 0)
        for event in events:
            self.history.append(event)
            self.cwd = event.get("cwd", self.cwd)
            if event.get("kind") == "project_context":
                self.project_context = event.get("content", "")
                self.project_root = event.get("root", "")

    @staticmethod
    def parse_question_mode(message: str) -> tuple[bool, str]:
        match = re.match(r"^/(?:proj|project)(?:\s+|$)", message.strip(), flags=re.IGNORECASE)
        if not match:
            return False, message.strip()
        return True, message.strip()[match.end():].strip()

    def handle_input(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return True
        if line in {"/quit", "/exit"}:
            return False
        if line in {"/stop", "/cancel"}:
            self.cancel_current()
            return True
        if line == "/help":
            self.write(
                "Info",
                "Ask anything, use /proj QUESTION for loaded project context, or use /stop, "
                "/learn, /run COMMAND, /clear, /help, and /quit.",
            )
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
        stage = "generating" if self.stream_text else ("reasoning" if self.reasoning_chars else "thinking")
        state = f"{stage} {spinner}" if self.busy else "ready"
        auto = "on" if self.config.autocomplete else "off"
        status = (
            f" {state} | context ~{tokens:,}/{self.config.context_window_tokens:,} tokens "
            f"| {mode} | autocomplete {auto} | [details]"
        )
        if self.project_root:
            status += " | project loaded"
        self._safe_add(screen, 1, 0, status, width - 1, curses.A_DIM)
        content_start = 3
        self._safe_add(screen, 2, 0, "─" * (width - 1), width - 1, curses.A_DIM)
        panel_height = 0
        if self.activity_expanded:
            panel_height = min(max(5, self.config.activity_panel_height), max(5, height // 3))
        available = max(1, height - content_start - 2 - panel_height)
        rendered: list[tuple[str, int]] = []
        colors = {"Buddy": 1, "You": 2, "Tool": 3, "Error": 4, "Shell": 5, "Info": 5}
        display_messages = list(self.messages)
        if self.stream_text and not self.stream_text.lstrip().lower().startswith("<tool"):
            display_messages.append(("Buddy", self.stream_text))
        for label, message in display_messages:
            prefix = f"{label}: "
            first = True
            for source_line in message.splitlines() or [""]:
                wrapped = textwrap.wrap(
                    source_line, width=max(10, width - len(prefix) - 1),
                    replace_whitespace=False, drop_whitespace=False,
                ) or [""]
                for part in wrapped:
                    rendered.append(((prefix if first else " " * len(prefix)) + part, colors.get(label, 5)))
                    first = False
            rendered.append(("", 0))
        for row, (line, color) in enumerate(rendered[-available:], start=content_start):
            attr = curses.color_pair(color) if curses.has_colors() and color else 0
            self._safe_add(screen, row, 0, line, width - 1, attr)

        if panel_height:
            panel_start = height - 2 - panel_height
            elapsed = time.monotonic() - self.request_started if self.busy and self.request_started else 0
            generated_tokens = int(len(self.stream_text) / self.config.chars_per_token_estimate)
            reasoning_tokens = int(self.reasoning_chars / self.config.chars_per_token_estimate)
            rate = generated_tokens / elapsed if elapsed > 0 else 0
            task = self.active_question.replace("\n", " ") if self.active_question else "No active request"
            details = [
                "─ Activity trace (F2/click to collapse) " + "─" * width,
                f" stage={self.active_kind or 'idle'}  elapsed={elapsed:.1f}s  request-context≈{self.request_context_tokens:,} tokens",
                f" progress: reasoning≈{reasoning_tokens:,} tokens  answer≈{generated_tokens:,} tokens  output≈{rate:.1f} token/s",
                f" cwd: {self.active_cwd}",
                f" task: {task}",
            ]
            details.extend(f" · {line}" for line in list(self.activity_log)[-(panel_height - 5):])
            for offset, line in enumerate(details[:panel_height]):
                self._safe_add(screen, panel_start + offset, 0, line, width - 1, curses.A_DIM)

        prompt_row = height - 1
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
        curses.mousemask(curses.BUTTON1_CLICKED)
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
                    if kind == "activity":
                        request_id, activity_kind, size = message
                        if request_id == self.active_request_id and activity_kind == "reasoning":
                            if not self.reasoning_started:
                                self.reasoning_started = time.monotonic()
                                self.activity_log.append(
                                    f"Reasoning stream began after {self.reasoning_started - self.request_started:.1f}s"
                                )
                            self.reasoning_chars += size
                    elif kind == "delta":
                        request_id, delta = message
                        if request_id == self.active_request_id:
                            if not self.first_delta_at:
                                self.first_delta_at = time.monotonic()
                                self.activity_log.append(
                                    f"First token after {self.first_delta_at - self.request_started:.1f}s"
                                )
                            self.stream_text += delta
                    elif kind == "response":
                        request_id, response = message
                        if request_id != self.active_request_id:
                            continue
                        self.busy = False
                        self.active_kind = ""
                        self.activity_log.append(
                            f"Answer completed: ~{int(len(response) / self.config.chars_per_token_estimate):,} tokens"
                        )
                        self.handle_model_response(response)
                        self.stream_text = ""
                    elif kind == "tool":
                        request_id, command, cwd, original, result = message
                        if request_id != self.active_request_id:
                            continue
                        self.busy = False
                        self.active_kind = ""
                        self.write("Tool", f"$ {command}\n{result.output}")
                        self.activity_log.append(f"Tool exited {result.returncode}: {command}")
                        append_event(self.events, "tool_result", {
                            "command": command, "output": result.output,
                            "status": result.returncode, "cwd": cwd, "source": "model-live",
                        })
                        followup = (
                            f"Original task: {original}\n\nYou requested `{command}`. It exited "
                            f"{result.returncode}. Output:\n{result.output}\n\nContinue solving the original task. "
                            "Request another tool whenever more evidence is useful."
                        )
                        self.request("ask", followup, continuation=True, cwd=cwd)
                    elif kind == "tool_error":
                        request_id, command, error = message
                        if request_id != self.active_request_id:
                            continue
                        self.busy = False
                        self.active_kind = ""
                        self.write("Error", f"Buddy tool request denied: {error}")
                        self.activity_log.append(f"Tool denied: {command}")
                        append_event(self.events, "tool_denied", {"command": command, "message": error})
                        original = self.active_question or "Review the latest terminal activity."
                        self.request(
                            "ask",
                            f"Original task: {original}\n\nYour tool `{command}` was denied: {error}\n"
                            "Continue the task using one of the allowed read-only alternatives named "
                            "in that error. Do not repeat the denied command.",
                            continuation=True,
                            cwd=self.active_cwd,
                        )
                    elif kind == "project":
                        request_id, snapshot = message
                        if request_id != self.active_request_id:
                            continue
                        self.busy = False
                        self.active_kind = ""
                        self.stream_text = ""
                        self.project_context = snapshot.content
                        self.project_root = snapshot.root
                        self.activity_log.append(
                            f"Project indexed: {snapshot.included}/{snapshot.discovered} files loaded"
                        )
                        summary = (
                            f"Loaded {snapshot.included} of {snapshot.discovered} discovered files from "
                            f"{snapshot.root} ({len(snapshot.content):,} characters; "
                            f"{snapshot.deferred} deferred by context budget; {snapshot.excluded_directories} "
                            f"dependency/build/VCS directories excluded; {snapshot.skipped_binary} binary and "
                            f"{snapshot.skipped_sensitive} sensitive files skipped"
                            + ("; context filled" if snapshot.truncated else "") + ")."
                        )
                        self.write("Info", summary)
                        append_event(self.events, "project_context", {
                            "root": snapshot.root, "content": snapshot.content,
                            "included": snapshot.included, "discovered": snapshot.discovered,
                        })
                        if self.config.summarize_project_on_load:
                            self.request(
                                "ask",
                                "Study the loaded project context. Summarize the architecture, purpose, "
                                "important entry points, and anything risky or surprising. Retain this "
                                "project context for subsequent questions.",
                                project_mode=True,
                            )
                    else:
                        request_id, error = message
                        if request_id != self.active_request_id:
                            continue
                        self.busy = False
                        self.active_kind = ""
                        self.stream_text = ""
                        self.activity_log.append(f"Request failed: {error}")
                        self.write("Error", error)
            except queue.Empty:
                pass
            if not self.busy and self.pending:
                pending_kind, pending_prompt, pending_cwd, pending_project_mode = self.pending.popleft()
                self.request(
                    pending_kind, pending_prompt, cwd=pending_cwd,
                    project_mode=pending_project_mode,
                )

            self.spinner_frame += 1
            self.render(screen)
            try:
                key = screen.get_wch()
            except curses.error:
                time.sleep(0.08)
                continue
            if key == curses.KEY_RESIZE:
                continue
            if key == curses.KEY_MOUSE:
                try:
                    _mouse_id, _x, y, _z, _button = curses.getmouse()
                    if y <= 1:
                        self.activity_expanded = not self.activity_expanded
                except curses.error:
                    pass
                continue
            if key == curses.KEY_F2:
                self.activity_expanded = not self.activity_expanded
                continue
            if key in ("\n", "\r"):
                line, self.input_buffer = self.input_buffer, ""
                running = self.handle_input(line)
            elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                self.input_buffer = self.input_buffer[:-1]
            elif key == "\x03":
                self.input_buffer = ""
                self.cancel_current()
            elif isinstance(key, str) and key.isprintable():
                self.input_buffer += key
            time.sleep(0.08)
        return 0

    def run(self) -> int:
        return curses.wrapper(self._run_curses)
