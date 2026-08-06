from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Config
from .tools import READ_ONLY_COMMANDS


class ModelError(RuntimeError):
    pass


def _request_error(exc: urllib.error.URLError) -> ModelError:
    detail = str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", errors="replace").strip()
            if body:
                detail += f": {body[:2000]}"
        except OSError:
            pass
    return ModelError(f"model request failed: {detail}")


@dataclass(slots=True)
class ModelClient:
    config: Config
    _active_response: Any = field(default=None, init=False, repr=False)
    _response_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def cancel(self) -> None:
        """Close the active streaming response so compatible servers stop generation."""
        with self._response_lock:
            response = self._active_response
            self._active_response = None
        if response is not None:
            try:
                response.close()
            except OSError:
                pass

    def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.2,
        timeout: int | None = None,
    ) -> str:
        url = self.config.endpoint.rstrip("/") + "/chat/completions"
        body = json.dumps({
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }).encode()
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.config.request_timeout) as response:
                data: dict[str, Any] = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if isinstance(exc, urllib.error.URLError):
                raise _request_error(exc) from exc
            raise ModelError(f"model request failed: {exc}") from exc
        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("model returned an unexpected response") from exc

    def stream(
        self, messages: list[dict[str, str]], *, temperature: float = 0.2,
        timeout: int | None = None,
        activity_callback: Callable[[str, str], None] | None = None,
    ):
        """Yield content deltas from an OpenAI-compatible SSE response."""
        url = self.config.endpoint.rstrip("/") + "/chat/completions"
        body = json.dumps({
            "model": self.config.model, "messages": messages,
            "temperature": temperature, "stream": True,
        }).encode()
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        fallback = bytearray()
        response_obj = None
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.config.request_timeout) as response:
                response_obj = response
                with self._response_lock:
                    self._active_response = response
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        fallback.extend(raw_line)
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        event = json.loads(data)
                        choice = event["choices"][0]
                        delta = choice.get("delta", {})
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning and activity_callback:
                            activity_callback("reasoning", str(reasoning))
                        content = delta.get("content") or choice.get("text")
                        if content:
                            yield str(content)
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        continue
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            if isinstance(exc, urllib.error.URLError):
                raise _request_error(exc) from exc
            raise ModelError(f"model request failed: {exc}") from exc
        finally:
            with self._response_lock:
                if self._active_response is response_obj:
                    self._active_response = None
        if fallback:
            try:
                data = json.loads(fallback)
                content = data["choices"][0]["message"]["content"]
                if content:
                    yield str(content)
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                raise ModelError("model returned an unexpected streaming response") from exc

    def observe(self, transcript: str) -> str:
        prompt = (
            "Review this latest terminal activity. Reply with one brief, useful observation "
            "or next action. If it succeeded and there is nothing meaningful to add, reply "
            "with exactly SILENT. This is passive observation: do not request tools and do "
            "not begin an investigation.\n\n" + transcript
        )
        return self.complete([
            {"role": "system", "content": self.config.system_prompt},
            {"role": "user", "content": prompt},
        ])

    def observe_stream(self, transcript: str, *, activity_callback=None):
        prompt = (
            "Review this latest terminal activity. Reply with one brief, useful observation "
            "or next action. If it succeeded and there is nothing meaningful to add, reply "
            "with exactly SILENT. This is passive observation: do not request tools and do "
            "not begin an investigation.\n\n" + transcript
        )
        return self.stream([
            {"role": "system", "content": self.config.system_prompt},
            {"role": "user", "content": prompt},
        ], activity_callback=activity_callback)

    def ask(self, question: str, context: str) -> str:
        tools = ", ".join(sorted(READ_ONLY_COMMANDS))
        estimated_tokens = int(len(context) / self.config.chars_per_token_estimate)
        timeout = (
            self.config.long_context_timeout
            if estimated_tokens >= self.config.context_window_tokens // 2
            else self.config.request_timeout
        )
        return self.complete([
            {"role": "system", "content": self.config.system_prompt + (
                " When local inspection is necessary, request one command by replying only "
                "with <tool>command</tool>. Tool availability is enforced by the host. "
                f"Available read-only commands: {tools}. Prefer a tool over guessing. "
                "Tool commands are direct argv, not shell: never use redirects, pipes, globs, "
                "command substitution, or operators such as 2>&1. Do not repeat a failed tool "
                "with the same path unless new evidence shows that path exists. For the latest "
                "commit and its impact, prefer `git show --stat --oneline HEAD`, then `git show HEAD`."
            )},
            {"role": "user", "content": f"Terminal context:\n{context}\n\nQuestion:\n{question}"},
        ], timeout=timeout)

    def ask_stream(self, question: str, context: str, *, activity_callback=None):
        tools = ", ".join(sorted(READ_ONLY_COMMANDS))
        estimated_tokens = int(len(context) / self.config.chars_per_token_estimate)
        timeout = (
            self.config.long_context_timeout
            if estimated_tokens >= self.config.context_window_tokens // 2
            else self.config.request_timeout
        )
        return self.stream([
            {"role": "system", "content": self.config.system_prompt + (
                " When local inspection is necessary, request one command by replying only "
                "with <tool>command</tool>. Tool availability is enforced by the host. "
                f"Available read-only commands: {tools}. Prefer a tool over guessing. "
                "Tool commands are direct argv, not shell: never use redirects, pipes, globs, "
                "command substitution, or operators such as 2>&1. Do not repeat a failed tool "
                "with the same path unless new evidence shows that path exists. For the latest "
                "commit and its impact, prefer `git show --stat --oneline HEAD`, then `git show HEAD`."
            )},
            {"role": "user", "content": f"Terminal context:\n{context}\n\nQuestion:\n{question}"},
        ], timeout=timeout, activity_callback=activity_callback)

    def suggest(self, command_buffer: str, context: str) -> str:
        answer = self.complete([
            {"role": "system", "content": (
                "Complete a shell command. Return only the exact suffix to append to the "
                "current buffer, with no markdown, explanation, newline, or command execution."
            )},
            {"role": "user", "content": f"Recent context:\n{context}\n\nCurrent buffer:\n{command_buffer}"},
        ], temperature=0.0)
        return answer.splitlines()[0] if answer else ""
