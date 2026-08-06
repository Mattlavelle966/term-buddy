from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Config
from .tools import READ_ONLY_COMMANDS


class ModelError(RuntimeError):
    pass


@dataclass(slots=True)
class ModelClient:
    config: Config

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
            raise ModelError(f"model request failed: {exc}") from exc
        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("model returned an unexpected response") from exc

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

    def ask(self, question: str, context: str) -> str:
        tools = ", ".join(sorted(READ_ONLY_COMMANDS))
        estimated_tokens = len(context) // 4
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
                "with the same path unless new evidence shows that path exists."
            )},
            {"role": "user", "content": f"Terminal context:\n{context}\n\nQuestion:\n{question}"},
        ], timeout=timeout)

    def suggest(self, command_buffer: str, context: str) -> str:
        answer = self.complete([
            {"role": "system", "content": (
                "Complete a shell command. Return only the exact suffix to append to the "
                "current buffer, with no markdown, explanation, newline, or command execution."
            )},
            {"role": "user", "content": f"Recent context:\n{context}\n\nCurrent buffer:\n{command_buffer}"},
        ], temperature=0.0)
        return answer.splitlines()[0] if answer else ""
