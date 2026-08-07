from __future__ import annotations

import curses
import json
import os
import queue
import re
import subprocess
import textwrap
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .config import Config
from .diagnostics import plan_diagnostics
from .events import append_event, read_events
from .model import ModelClient, ModelError
from .memory import ProjectMemory
from .project import select_project_context
from .tools import ToolDenied, run_command


def load_logo() -> tuple[str, ...]:
    path = Path(__file__).resolve().parent.parent / "assets" / "term-buddy-logo.txt"
    try:
        return tuple(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return ()


class BuddyUI:
    def __init__(self, config: Config, events: Path, session: str, *, yolo: bool, proactive: bool):
        self.config = config
        self.events = events
        self.session = session
        self.yolo = yolo
        self.client = ModelClient(config)
        self.memory = ProjectMemory(self.events.parent / "memory.sqlite3")
        self.offset = 0
        self.history: deque[dict] = deque(maxlen=config.context_commands * 3)
        self.messages: deque[tuple[str, str]] = deque(maxlen=300)
        self.results: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.pending: deque[tuple[str, str, str, str]] = deque(maxlen=20)
        self.busy = False
        self.active_kind = ""
        self.request_serial = 0
        self.active_request_id = 0
        self.cwd = os.getcwd()
        self.input_buffer = ""
        self.spinner_frame = 0
        self.active_question = ""
        self.active_project_mode = "none"
        self.active_cwd = self.cwd
        self.project_context = ""
        self.project_root = ""
        self.stream_text = ""
        self.request_started = 0.0
        self.request_context_tokens = 0
        self.first_delta_at = 0.0
        self.reasoning_chars = 0
        self.reasoning_started = 0.0
        self.last_output_tokens = 0
        self.server_connected = False
        # Start compact even when an older config enabled the former permanent panel.
        self.activity_expanded = False
        self.activity_log: deque[str] = deque(maxlen=8)
        self.activity_path = self.events.parent / "activity.jsonl"
        self.failure_fingerprint = ""
        self.failure_count = 0
        self.tool_attempts: dict[str, int] = {}
        self.last_retrieval_sources: list[str] = []
        self.logo = load_logo()
        self.splash_until = 0.0
        self.splash_started = False
        self.splash_zoomed = False
        self.splash_next_check = 0.0
        self.tmux_pane = os.environ.get("TMUX_PANE", "")
        self.settings_open = False
        self.runtime_autocomplete = config.autocomplete
        self.watch_repeats = proactive
        self.context_watch = config.context_watch
        self.autocomplete_flag = self.events.parent / "autocomplete.enabled"
        self._write_autocomplete_flag()
        self.last_command = ""
        self.command_repeat_count = 0

    def _write_autocomplete_flag(self) -> None:
        try:
            self.autocomplete_flag.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.runtime_autocomplete:
                self.autocomplete_flag.write_text("on\n", encoding="utf-8")
                os.chmod(self.autocomplete_flag, 0o600)
            else:
                self.autocomplete_flag.unlink(missing_ok=True)
        except OSError:
            pass

    def set_runtime_autocomplete(self, enabled: bool) -> None:
        self.runtime_autocomplete = enabled
        self._write_autocomplete_flag()
        state = "on" if enabled else "off"
        self.trace("setting", f"autocomplete {state}")
        self.write("Info", f"Autocomplete is {state} for this session. Press Shift-Tab in the shell.")

    def set_repeat_watch(self, enabled: bool) -> None:
        self.watch_repeats = enabled
        self.failure_fingerprint = ""
        self.failure_count = 0
        self.last_command = ""
        self.command_repeat_count = 0
        state = "on" if enabled else "off"
        self.trace("setting", f"repeat watch {state}")
        self.write("Info", f"Repeated-command hints are {state} for this session.")

    def set_context_watch(self, enabled: bool) -> None:
        self.context_watch = enabled
        state = "on" if enabled else "off"
        self.trace("setting", f"context questions {state}")
        self.write(
            "Info",
            f"Context questions are {state}. Valid commands stay silent; obvious questions typed "
            "without `buddy` are answered.",
        )

    def handle_runtime_setting(self, line: str) -> bool:
        autocomplete = re.fullmatch(r"/?autocomplete(?:\s+(on|off|status))?", line.strip(), re.IGNORECASE)
        if autocomplete:
            action = (autocomplete.group(1) or "status").lower()
            if action != "status":
                self.set_runtime_autocomplete(action == "on")
            else:
                state = "on" if self.runtime_autocomplete else "off"
                self.write("Info", f"Autocomplete is {state}. Toggle with F2 then A; use Shift-Tab in the shell.")
            return True
        watch = re.fullmatch(r"/?watch(?:\s+(on|off|status))?", line.strip(), re.IGNORECASE)
        if watch:
            action = (watch.group(1) or "status").lower()
            if action != "status":
                self.set_repeat_watch(action == "on")
            else:
                state = "on" if self.watch_repeats else "off"
                self.write("Info", f"Repeated-command hints are {state}.")
            return True
        context = re.fullmatch(r"/?context(?:\s+(on|off|status))?", line.strip(), re.IGNORECASE)
        if context:
            action = (context.group(1) or "status").lower()
            if action != "status":
                self.set_context_watch(action == "on")
            else:
                state = "on" if self.context_watch else "off"
                self.write("Info", f"Context questions are {state}.")
            return True
        return False

    def splash_fits(self, height: int, width: int) -> bool:
        # Crop on small terminals instead of silently hiding the artwork.
        return bool(self.logo and height > 2 and width > 2)

    def _tmux_value(self, value: str) -> str:
        try:
            completed = subprocess.run(
                ["tmux", "display-message", "-p", "-t", self.tmux_pane or self.session, value],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=1,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return completed.stdout.strip() if completed.returncode == 0 else ""

    def _begin_startup_splash(self) -> None:
        self.splash_started = True
        self.splash_until = time.monotonic() + 4.0
        if not self.tmux_pane or self._tmux_value("#{window_zoomed_flag}") != "0":
            return
        try:
            completed = subprocess.run(
                ["tmux", "resize-pane", "-Z", "-t", self.tmux_pane],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1,
            )
            self.splash_zoomed = completed.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass

    def _finish_startup_splash(self) -> None:
        self.splash_until = 0.0
        if not self.splash_zoomed:
            return
        # Avoid re-zooming if the user already restored the split manually.
        if self._tmux_value("#{window_zoomed_flag}") == "1":
            try:
                subprocess.run(
                    ["tmux", "resize-pane", "-Z", "-t", self.tmux_pane],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        self.splash_zoomed = False

    def _update_startup_splash(self) -> None:
        now = time.monotonic()
        if not self.splash_started:
            if not self.tmux_pane:
                self._begin_startup_splash()
                return
            if now < self.splash_next_check:
                return
            self.splash_next_check = now + 0.2
            attached = self._tmux_value("#{session_attached}")
            if attached and attached != "0":
                self._begin_startup_splash()
            return
        if self.splash_until and now >= self.splash_until:
            self._finish_startup_splash()

    def render_splash(self, screen: curses.window, height: int, width: int) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        logo_width = max(map(len, self.logo), default=0)
        top = (height - len(self.logo) - 1) // 2
        left = (width - logo_width) // 2
        attr = curses.color_pair(1) | curses.A_BOLD if curses.has_colors() else curses.A_BOLD
        for offset, line in enumerate(self.logo):
            row = top + offset
            if not 0 <= row < height:
                continue
            source_start = max(0, -left)
            column = max(0, left)
            visible = line[source_start:source_start + max(0, width - column - 1)]
            self._safe_add(screen, row, column, visible, width - column - 1, attr)
        hint = "TERM BUDDY · LOCAL TERMINAL COPILOT · press any key"
        hint_row = top + len(self.logo) + 1
        if 0 <= hint_row < height:
            self._safe_add(
                screen, hint_row, max(0, (width - len(hint)) // 2),
                hint, min(len(hint), width - 1), curses.A_DIM,
            )
        screen.refresh()

    def trace(self, stage: str, detail: str) -> None:
        """Record an observable harness action in both the pane and a structured log."""
        line = f"{stage.upper()}  {detail}".strip()
        self.activity_log.append(line)
        try:
            self.activity_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.activity_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "time": time.time(), "stage": stage, "detail": detail,
                    "cwd": self.active_cwd, "request_id": self.active_request_id,
                }, ensure_ascii=False) + "\n")
            os.chmod(self.activity_path, 0o600)
        except OSError:
            pass

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

    def request_context(self, kind: str, prompt: str, *, project_mode: str = "none") -> str:
        self.last_retrieval_sources = []
        if kind == "observe":
            signal = f"Harness signal: {prompt}\n\n" if prompt else ""
            return signal + self.terminal_context()
        conversation = self.conversation_context()
        lightweight = "\n\n".join(part for part in (self.terminal_context(), conversation) if part)
        root = self.memory.root_for(self.active_cwd)
        should_retrieve = root and not self.is_operational_question(prompt) and not self.is_rewrite_followup(prompt)
        if should_retrieve:
            query_tokens = 8000
            if re.search(r"\b(architecture|comprehensive|whole|entire|across|overview)\b", prompt, re.IGNORECASE):
                query_tokens = self.config.automatic_context_tokens
            elif re.search(r"\b[\w.-]+\.(?:py|js|ts|vue|go|rs|java|c|cpp|h)\b", prompt, re.IGNORECASE):
                query_tokens = 12000
            selected, sources = self.memory.retrieve(
                root, prompt,
                int(query_tokens * self.config.chars_per_token_estimate),
            )
            if sources:
                self.project_root = root
                self.last_retrieval_sources = sources
                self.trace("retrieve", f"{len(sources)} files · {', '.join(sources[:3])}")
                return "\n\n".join(part for part in (selected, lightweight) if part)
        # Old session snapshots remain readable, but are no longer sent wholesale.
        if project_mode in {"project", "full"} and self.project_context:
            selected = select_project_context(
                self.project_context, prompt,
                int(self.config.automatic_context_tokens * self.config.chars_per_token_estimate),
            )
            return "\n\n".join(part for part in (selected, lightweight) if part)
        return lightweight

    @staticmethod
    def is_rewrite_followup(prompt: str) -> bool:
        return bool(re.search(
            r"\b(shorter|summari[sz]e|condense|rewrite|rephrase|tl;?dr|previous answer|"
            r"last answer|that answer|your answer)\b",
            prompt,
            flags=re.IGNORECASE,
        ))

    @staticmethod
    def is_operational_question(prompt: str) -> bool:
        return bool(re.search(
            r"\b(git|commit|commits|branch|revision|merge|diff|uncommitted|gpu|cpu|memory|"
            r"disk|process|ports?|services?|journal|logs?|exit code)\b",
            prompt,
            flags=re.IGNORECASE,
        ))

    @staticmethod
    def failure_signature(command: str, status: int, output: str) -> str:
        executable = command.strip().split(maxsplit=1)[0] if command.strip() else "unknown"
        lines = [line.strip().lower() for line in output.splitlines() if line.strip()]
        diagnostic = next((
            line for line in reversed(lines)
            if re.search(r"error|not found|no such|denied|failed|invalid|unknown command", line)
        ), lines[-1] if lines else "")
        diagnostic = re.sub(r"\b\d+(?:\.\d+)?\b", "#", diagnostic)
        diagnostic = re.sub(r"\s+", " ", diagnostic)[-240:]
        return f"{status}:{executable}:{diagnostic}"

    @staticmethod
    def looks_like_question(command: str) -> bool:
        text = " ".join(command.strip().split()).lower()
        return bool(
            text.endswith("?")
            or re.match(
                r"^(what|why|how|where|when|who|which)\b|"
                r"^(can|could|would|will|do|does|did|is|are|should)\s+(you|i|we|there|this|that)\b|"
                r"^(tell me|explain|help me|show me)\b",
                text,
            )
        )

    def request(
        self, kind: str, prompt: str = "", *, continuation: bool = False,
        cwd: str | None = None, project_mode: str | None = None,
    ) -> None:
        request_cwd = cwd or self.cwd
        requested_project_mode = (
            self.active_project_mode if continuation and project_mode is None
            else project_mode or "none"
        )
        rewrite_followup = self.is_rewrite_followup(prompt)
        if self.busy:
            if self.active_kind == "observe" and kind == "ask":
                self.cancel_current(silent=True)
            elif kind == "ask" and not continuation and self.config.interrupt_on_new_question:
                previous = self.active_question
                partial = self.stream_text[-8000:]
                self.cancel_current(silent=True)
                if previous or partial:
                    prompt = (
                        f"Previous request: {previous}\n\nPartial answer before interruption:\n"
                        f"{partial or '[no answer tokens yet]'}\n\nNew user instruction: {prompt}"
                    )
                self.write("Info", "Interrupted the previous Buddy task for the new question.")
            else:
                if kind == "ask":
                    self.pending.append((kind, prompt, request_cwd, requested_project_mode))
                return
        if kind == "ask" and not continuation:
            self.tool_attempts.clear()
            if rewrite_followup:
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
        self.last_output_tokens = 0
        context = self.request_context(kind, prompt, project_mode=requested_project_mode)
        self.stream_text = ""
        self.request_started = time.monotonic()
        self.first_delta_at = 0.0
        self.reasoning_chars = 0
        self.reasoning_started = 0.0
        self.server_connected = False
        self.request_context_tokens = int(len(context) / self.config.chars_per_token_estimate)
        scope = {
            "full": "full project + terminal",
            "project": "focused project + terminal",
            "none": "lightweight terminal + chat",
        }[requested_project_mode]
        if self.last_retrieval_sources:
            scope = f"retrieved {len(self.last_retrieval_sources)} files"
        self.trace("model", f"{kind} · {scope} · ~{self.request_context_tokens:,} tokens")

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
            except Exception as exc:
                self.results.put(("error", (request_id, f"unexpected model worker error: {exc}")))

        threading.Thread(target=worker, daemon=True).start()

    def ask_with_evidence(self, prompt: str, cwd: str, project_mode: str = "none") -> None:
        commands = plan_diagnostics(prompt)
        if not commands:
            self.request("ask", prompt, cwd=cwd, project_mode=project_mode)
            return
        if self.busy:
            self.cancel_current(silent=True)
        self.request_serial += 1
        request_id = self.request_serial
        self.active_request_id = request_id
        self.active_kind = "inspect"
        self.active_question = prompt
        self.active_cwd = cwd
        self.busy = True
        self.request_started = time.monotonic()
        self.trace("inspect", f"{len(commands)} deterministic diagnostic{'s' if len(commands) != 1 else ''}")

        def worker() -> None:
            evidence: list[str] = []
            for command in commands:
                self.results.put(("inspect_activity", (request_id, command)))
                try:
                    result = run_command(command, cwd=cwd, yolo=False)
                    evidence.append(f"$ {command}\nexit={result.returncode}\n{result.output}")
                except (ToolDenied, ValueError) as exc:
                    evidence.append(f"$ {command}\nDENIED: {exc}")
            self.results.put(("inspected", (request_id, prompt, cwd, project_mode, "\n\n".join(evidence))))

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
            self.trace("cancel", previous_kind or "queued")
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
        self.last_output_tokens = 0
        self.request_serial += 1
        request_id = self.request_serial
        self.active_request_id = request_id
        self.active_kind = "project"
        root = self.cwd
        self.write("Info", f"Indexing local project memory under {root}...")
        self.trace("index", root)

        def worker() -> None:
            try:
                self.results.put(("memory", (request_id, self.memory.index(root))))
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
            if self.handle_runtime_setting(raw_message):
                return
            if self.is_project_trigger(raw_message):
                self.learn_project()
            else:
                project_mode, message = self.parse_question_mode(raw_message)
                if project_mode != "none" and not message:
                    self.write("Error", "Usage: buddy QUESTION")
                    return
                self.ask_with_evidence(message, event.get("cwd", self.cwd), project_mode)
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
            if self.context_watch and status in {126, 127} and self.looks_like_question(command):
                self.trace("question", "natural-language command recovered")
                self.write("Info", "That looks like a question; answering it with recent terminal context.")
                self.ask_with_evidence(command, event.get("cwd", self.cwd))
                return
            normalized_command = " ".join(command.strip().split())
            if normalized_command and normalized_command == self.last_command:
                self.command_repeat_count += 1
            else:
                self.last_command = normalized_command
                self.command_repeat_count = 1 if normalized_command else 0
            if status == 0:
                self.failure_fingerprint = ""
                self.failure_count = 0
                if self.watch_repeats and self.command_repeat_count == 2:
                    self.trace("repeat", f"{normalized_command} · 2×")
                    self.write("Info", "You ran the same command twice; asking Buddy for one useful hint.")
                    self.request(
                        "observe",
                        f"The user ran this exact command twice consecutively even though it succeeded: "
                        f"{normalized_command}. Offer one short, practical hint that may help; do not use tools.",
                    )
                return
            output = str(event.get("output", ""))
            executable = command.strip().split(maxsplit=1)[0] if command.strip() else "unknown"
            fingerprint = self.failure_signature(command, status, output)
            if fingerprint == self.failure_fingerprint:
                self.failure_count += 1
            else:
                self.failure_fingerprint = fingerprint
                self.failure_count = 1
            self.trace("error", f"{executable} failed · repeat {self.failure_count}")
            repeated_error = self.failure_count == self.config.error_repeat_threshold
            repeated_command = self.command_repeat_count == 2
            if self.watch_repeats and (repeated_error or repeated_command):
                self.write("Info", f"Repeated failure detected ({self.failure_count}×); asking Buddy for one hint.")
                self.request(
                    "observe",
                    f"The user appears stuck after repeating `{normalized_command}`. "
                    "Offer one short, practical hint based on the captured errors; do not use tools.",
                )
        elif kind == "tool_result" and event.get("source") != "model-live":
            self.write("Tool", f"$ {event.get('command')}\n{event.get('output', '')}")

    def handle_model_response(self, message: str) -> None:
        command = self.extract_tool_request(message)
        if not command:
            if message.strip().upper() != "SILENT":
                self.write("Buddy", message)
                append_event(self.events, "assistant", {"message": message})
            return
        normalized_command = " ".join(command.split())
        attempts = self.tool_attempts.get(normalized_command, 0) + 1
        self.tool_attempts[normalized_command] = attempts
        if attempts > 1:
            self.trace("loop", f"blocked repeated tool · {normalized_command}")
            self.write("Error", f"Stopped a repeated tool loop: {normalized_command}")
            return
        self.trace("tool", f"requested · {command}")
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

    @staticmethod
    def extract_tool_request(message: str) -> str:
        tagged = re.search(r"<tool>\s*(.*?)\s*</tool>", message, flags=re.DOTALL | re.IGNORECASE)
        if tagged:
            return tagged.group(1).strip().strip("`")
        labeled = re.search(
            r"(?:next\s+tool\s+request|tool\s+request|run\s+this\s+command)\s*:?\s*"
            r"(?:\*\*)?\s*```(?:bash|sh|shell)?\s*\n?([^\n`]+)",
            message,
            flags=re.IGNORECASE,
        )
        if labeled:
            return labeled.group(1).strip().removeprefix("$ ")
        return ""

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
    def parse_question_mode(message: str) -> tuple[str, str]:
        full = re.match(r"^/(?:proj|project)-full(?:\s+|$)", message.strip(), flags=re.IGNORECASE)
        if full:
            return "full", message.strip()[full.end():].strip()
        match = re.match(r"^/(?:proj|project)(?:\s+|$)", message.strip(), flags=re.IGNORECASE)
        if not match:
            return "none", message.strip()
        return "project", message.strip()[match.end():].strip()

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
                "Ask normally; project memory is retrieved automatically. Commands: /stop, "
                "/learn, /run COMMAND, /autocomplete on|off, /watch on|off, /context on|off, /log, /clear, "
                "/help, and /quit. F2 opens live settings.",
            )
            return True
        if self.handle_runtime_setting(line):
            return True
        if line == "/log":
            self.write("Info", f"Structured harness log: {self.activity_path}")
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

    def render_settings(self, screen: curses.window, height: int, width: int) -> None:
        lines = [
            "F2 settings",
            f"[A] Complete {'ON' if self.runtime_autocomplete else 'OFF'}",
            f"[W] Repeats  {'ON' if self.watch_repeats else 'OFF'}",
            f"[Q] Questions {'ON' if self.context_watch else 'OFF'}",
            f"[D] Details  {'ON' if self.activity_expanded else 'OFF'}",
            "F2/Esc close",
        ]
        box_width = min(width - 2, max(len(line) for line in lines) + 4)
        left = max(0, (width - box_width) // 2)
        top = max(0, (height - len(lines) - 2) // 2)
        self._safe_add(screen, top, left, "┌" + "─" * (box_width - 2) + "┐", box_width, curses.A_BOLD)
        for offset, line in enumerate(lines, start=1):
            body = f" {line}".ljust(box_width - 2)
            self._safe_add(screen, top + offset, left, "│" + body + "│", box_width, curses.A_BOLD if offset == 1 else 0)
        self._safe_add(screen, top + len(lines) + 1, left, "└" + "─" * (box_width - 2) + "┘", box_width, curses.A_BOLD)
        screen.refresh()

    def render(self, screen: curses.window) -> None:
        screen.erase()
        height, width = screen.getmaxyx()
        if self.splash_until and time.monotonic() < self.splash_until and self.splash_fits(height, width):
            self.render_splash(screen, height, width)
            return
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        if height < 7 or width < 24:
            self._safe_add(screen, 0, 0, "Term Buddy: pane too small", width - 1, curses.A_BOLD)
            screen.refresh()
            return
        mode = "RW" if self.yolo else "RO"
        tokens = self.estimated_tokens()
        spinner = "|/-\\"[self.spinner_frame % 4]
        stage = (
            "ready" if not self.busy else
            "generating" if self.stream_text else
            "reasoning" if self.reasoning_chars else
            "prefill" if self.server_connected else
            "waiting-server"
        )
        state = f"{stage} {spinner}" if self.busy else "ready"
        self._safe_add(screen, 0, 0, f" Buddy · {state} · {mode}", width - 1, curses.A_BOLD)
        completion = "comp+" if self.runtime_autocomplete else "comp-"
        memory_status = "mem+" if self.project_root else "mem-"
        questions = "ask+" if self.context_watch else "ask-"
        self._safe_add(screen, 1, 0, f" {self.config.model} · {memory_status} · {completion} · {questions} · F2", width - 1, curses.A_DIM)
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
            generated_tokens = (
                int(len(self.stream_text) / self.config.chars_per_token_estimate)
                if self.stream_text else self.last_output_tokens
            )
            reasoning_tokens = int(self.reasoning_chars / self.config.chars_per_token_estimate)
            rate = generated_tokens / elapsed if elapsed > 0 else 0
            task = self.active_question.replace("\n", " ") if self.active_question else "No active request"
            details = [
                "─ Activity trace (F2 then D to collapse) " + "─" * width,
                f" stage={stage}/{self.active_kind or 'idle'}  elapsed={elapsed:.1f}s  request-context≈{self.request_context_tokens:,} tokens",
                f" progress: reasoning≈{reasoning_tokens:,} tokens  answer≈{generated_tokens:,} tokens  output≈{rate:.1f} token/s",
                f" cwd: {self.active_cwd}",
                f" task: {task}",
            ]
            details.extend(f" · {line}" for line in list(self.activity_log)[-(panel_height - 5):])
            for offset, line in enumerate(details[:panel_height]):
                self._safe_add(screen, panel_start + offset, 0, line, width - 1, curses.A_DIM)

        prompt_row = height - 1
        bottom = (
            f" {self.activity_log[-1]}" if self.activity_log and not panel_height
            else "─" * (width - 1)
        )
        self._safe_add(screen, prompt_row - 1, 0, bottom, width - 1, curses.A_DIM)
        prompt = "> " + self.input_buffer
        self._safe_add(screen, prompt_row, 0, prompt, width - 1, curses.A_BOLD)
        try:
            screen.move(prompt_row, min(width - 2, len(prompt)))
        except curses.error:
            pass
        if self.settings_open:
            self.render_settings(screen, height, width)
            return
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
            self._update_startup_splash()
            events, self.offset = read_events(self.events, self.offset)
            for event in events:
                self.handle_event(event)
            try:
                while True:
                    kind, message = self.results.get_nowait()
                    if kind == "inspect_activity":
                        request_id, command = message
                        if request_id == self.active_request_id:
                            self.trace("tool", command)
                    elif kind == "inspected":
                        request_id, prompt, cwd, project_mode, evidence = message
                        if request_id != self.active_request_id:
                            continue
                        self.busy = False
                        self.active_kind = ""
                        self.request(
                            "ask",
                            f"{prompt}\n\nAuthoritative local diagnostic evidence:\n{evidence}",
                            cwd=cwd, project_mode=project_mode,
                        )
                    elif kind == "activity":
                        request_id, activity_kind, size = message
                        if request_id == self.active_request_id:
                            if activity_kind == "request_sent":
                                self.trace("model", "request sent")
                            elif activity_kind == "connected":
                                self.server_connected = True
                                self.trace("prefill", "server connected · waiting for first token")
                            elif activity_kind == "reasoning":
                                if not self.reasoning_started:
                                    self.reasoning_started = time.monotonic()
                                    self.trace("reason", f"stream began after {self.reasoning_started - self.request_started:.1f}s")
                                self.reasoning_chars += size
                    elif kind == "delta":
                        request_id, delta = message
                        if request_id == self.active_request_id:
                            if not self.first_delta_at:
                                self.first_delta_at = time.monotonic()
                                self.trace("generate", f"first token after {self.first_delta_at - self.request_started:.1f}s")
                            self.stream_text += delta
                    elif kind == "response":
                        request_id, response = message
                        if request_id != self.active_request_id:
                            continue
                        self.busy = False
                        self.active_kind = ""
                        self.trace("done", f"~{int(len(response) / self.config.chars_per_token_estimate):,} tokens")
                        self.last_output_tokens = int(
                            len(response) / self.config.chars_per_token_estimate
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
                        self.trace("tool", f"exit {result.returncode} · {command}")
                        append_event(self.events, "tool_result", {
                            "command": command, "output": result.output,
                            "status": result.returncode, "cwd": cwd, "source": "model-live",
                        })
                        followup = (
                            f"Original task: {original}\n\nYou requested `{command}`. It exited "
                            f"{result.returncode}. Output:\n{result.output}\n\nContinue solving the original task. "
                            "Request another tool whenever more evidence is useful."
                        )
                        self.request(
                            "ask", followup, continuation=True, cwd=cwd,
                            project_mode="none",
                        )
                    elif kind == "tool_error":
                        request_id, command, error = message
                        if request_id != self.active_request_id:
                            continue
                        self.busy = False
                        self.active_kind = ""
                        self.write("Error", f"Buddy tool request denied: {error}")
                        self.trace("denied", command)
                        append_event(self.events, "tool_denied", {"command": command, "message": error})
                        original = self.active_question or "Review the latest terminal activity."
                        self.request(
                            "ask",
                            f"Original task: {original}\n\nYour tool `{command}` was denied: {error}\n"
                            "Continue the task using one of the allowed read-only alternatives named "
                            "in that error. Do not repeat the denied command.",
                            continuation=True,
                            cwd=self.active_cwd,
                            project_mode="none",
                        )
                    elif kind == "memory":
                        request_id, report = message
                        if request_id != self.active_request_id:
                            continue
                        self.busy = False
                        self.active_kind = ""
                        self.stream_text = ""
                        self.project_context = ""
                        self.project_root = report.root
                        self.trace("indexed", f"{report.indexed} changed · {report.unchanged} unchanged")
                        summary = (
                            f"Remembered {report.discovered} files from {report.root}: "
                            f"{report.indexed} changed, {report.unchanged} unchanged, {report.removed} removed; "
                            f"{report.excluded_directories} dependency/build/VCS directories excluded, "
                            f"{report.skipped_binary} binary and {report.skipped_sensitive} sensitive skipped. "
                            "Future questions retrieve only relevant files automatically."
                        )
                        self.write("Info", summary)
                        append_event(self.events, "project_context", {
                            "root": report.root, "content": "", "discovered": report.discovered,
                        })
                    else:
                        request_id, error = message
                        if request_id != self.active_request_id:
                            continue
                        self.busy = False
                        self.active_kind = ""
                        self.stream_text = ""
                        self.trace("error", error)
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
            # Zooming the pane generates KEY_RESIZE. It must redraw the logo,
            # not count as the user's "press any key" dismissal.
            if key == curses.KEY_RESIZE:
                continue
            if self.splash_until and time.monotonic() < self.splash_until:
                self._finish_startup_splash()
                continue
            if self.settings_open:
                if key in (curses.KEY_F2, "\x1b"):
                    self.settings_open = False
                elif key in ("a", "A"):
                    self.set_runtime_autocomplete(not self.runtime_autocomplete)
                elif key in ("w", "W"):
                    self.set_repeat_watch(not self.watch_repeats)
                elif key in ("q", "Q"):
                    self.set_context_watch(not self.context_watch)
                elif key in ("d", "D"):
                    self.activity_expanded = not self.activity_expanded
                continue
            if key == curses.KEY_MOUSE:
                try:
                    _mouse_id, _x, y, _z, _button = curses.getmouse()
                    if y <= 1:
                        self.settings_open = True
                except curses.error:
                    pass
                continue
            if key == curses.KEY_F2:
                self.settings_open = True
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
        try:
            return curses.wrapper(self._run_curses)
        finally:
            self._finish_startup_splash()
