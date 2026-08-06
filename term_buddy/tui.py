from __future__ import annotations

import os
import queue
import re
import select
import sys
import textwrap
import threading
import time
from collections import deque
from pathlib import Path

from .config import Config
from .events import append_event, read_events
from .model import ModelClient, ModelError
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
        self.results: queue.Queue[tuple[str, str]] = queue.Queue()
        self.pending: deque[tuple[str, str]] = deque(maxlen=20)
        self.busy = False
        self.cwd = os.getcwd()

    def write(self, label: str, message: str) -> None:
        width = max(28, os.get_terminal_size().columns - 3)
        color = {"Buddy": "\033[36m", "You": "\033[33m", "Tool": "\033[35m", "Error": "\033[31m"}.get(label, "\033[2m")
        print(f"\n{color}{label}:\033[0m", flush=False)
        print(textwrap.fill(message.strip(), width=width, replace_whitespace=False), flush=True)

    def context(self) -> str:
        chunks: list[str] = []
        for event in list(self.history)[-self.config.context_commands:]:
            if event.get("kind") == "command_finished":
                chunks.append(
                    f"$ {event.get('command', '')}\nexit={event.get('status')} cwd={event.get('cwd')}\n"
                    f"{str(event.get('output', ''))[-self.config.max_output_chars:]}"
                )
        return "\n\n".join(chunks)[-self.config.max_output_chars:]

    def request(self, kind: str, prompt: str = "") -> None:
        if self.busy:
            if kind == "ask":
                self.pending.append((kind, prompt))
            return
        self.busy = True
        context = self.context()

        def worker() -> None:
            try:
                response = self.client.observe(context) if kind == "observe" else self.client.ask(prompt, context)
                self.results.put(("response", response))
            except ModelError as exc:
                self.results.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def handle_event(self, event: dict) -> None:
        self.history.append(event)
        self.cwd = event.get("cwd", self.cwd)
        kind = event.get("kind")
        if kind == "question":
            self.write("You", event.get("message", ""))
            self.request("ask", event.get("message", ""))
        elif kind == "command_finished":
            command = event.get("command", "")
            status = event.get("status", 0)
            self.write("Shell", f"[{status}] $ {command}")
            if self.proactive and (status != 0 or command):
                self.request("observe")
        elif kind == "tool_result":
            self.write("Tool", f"$ {event.get('command')}\n{event.get('output', '')}")

    def handle_model_response(self, message: str) -> None:
        match = re.fullmatch(r"\s*<tool>(.*?)</tool>\s*", message, flags=re.DOTALL)
        if not match:
            if message.upper() != "SILENT":
                self.write("Buddy", message)
                append_event(self.events, "assistant", {"message": message})
            return
        command = match.group(1).strip()
        if not self.config.tools:
            self.write("Error", "Buddy tool requests are disabled in configuration.")
            return
        try:
            result = run_command(command, cwd=self.cwd, yolo=self.yolo)
        except (ToolDenied, ValueError) as exc:
            self.write("Error", f"Buddy tool request denied: {exc}")
            append_event(self.events, "tool_denied", {"command": command, "message": str(exc)})
            return
        self.write("Tool", f"$ {command}\n{result.output}")
        append_event(self.events, "tool_result", {
            "command": command, "output": result.output,
            "status": result.returncode, "cwd": self.cwd, "source": "model",
        })
        self.request("ask", (
            f"You requested `{command}`. It exited {result.returncode}. Here is its output:\n"
            f"{result.output}\nNow answer the original debugging question concisely."
        ))

    def handle_input(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return True
        if line in {"/quit", "/exit"}:
            return False
        if line == "/help":
            self.write("Info", "Ask anything, or use /run COMMAND, /clear, /help, and /quit.")
            return True
        if line == "/clear":
            print("\033[2J\033[H", end="", flush=True)
            return True
        if line.startswith("/run "):
            if not self.config.tools:
                self.write("Error", "Tools are disabled in configuration.")
                return True
            command = line[5:].strip()
            try:
                result = run_command(command, cwd=self.cwd, yolo=self.yolo)
                append_event(self.events, "tool_result", {
                    "command": command, "output": result.output, "status": result.returncode, "cwd": self.cwd,
                })
            except (ToolDenied, ValueError) as exc:
                self.write("Error", str(exc))
            return True
        append_event(self.events, "question", {"message": line, "cwd": self.cwd, "source": "buddy-pane"})
        return True

    def run(self) -> int:
        print("\033[2J\033[H\033[1;36mTerm Buddy\033[0m")
        mode = "YOLO read/write" if self.yolo else "read-only tools"
        print(f"{mode} • model {self.config.model} • /help for commands")
        print("Ask here, or run `buddy your question` in the shell. Shift-Tab completes a command.")
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
                    elif kind == "error":
                        self.write("Error", message)
            except queue.Empty:
                pass
            if not self.busy and self.pending:
                pending_kind, pending_prompt = self.pending.popleft()
                self.request(pending_kind, pending_prompt)
            readable, _, _ = select.select([sys.stdin], [], [], 0.2)
            if readable:
                line = sys.stdin.readline()
                if not line:
                    break
                running = self.handle_input(line)
            time.sleep(0.05)
        return 0
